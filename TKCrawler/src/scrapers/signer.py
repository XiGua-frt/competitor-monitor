"""TikTokSigner: X-Bogus signature generator (pure Python, stdlib only).

Reverse-engineering basis: see docs/reverse/video-api.md. The algorithm is
reproduced from public open-source implementations (armxe/tiktok-api
Web/bogus.py, cross-checked against the yvbbrjdr gist and the
huuhuybn/X-Gorgon README). Constants have NOT been validated against the
live TikTok webmssdk JS — treat the output as "assumed, pending on-device
verification".

Injectability contract (task card requirement):
- Every algorithm constant lives in DEFAULT_CONFIG and can be overridden via
  the `config` constructor argument or update_config() when TikTok updates
  its JS (e.g. new magic constant / charset).
- The whole signing step can be replaced by passing `sign_func=callable`
  (e.g. a bridge that calls a real browser session), so a parameter drift
  never requires touching this file.
"""

import base64
import hashlib
import time
from typing import Callable
from urllib.parse import urlparse

# Custom base64 alphabet used by X-Bogus (public knowledge, R4 in docs).
DEFAULT_CHARSET = (
    'Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe'
)

DEFAULT_CONFIG = {
    'charset': DEFAULT_CHARSET,
    # Magic constant embedded as big-endian 4 bytes (armxe variant).
    'magic': 536919696,
    # RC4 keys.
    'rc4_ua_key': [0, 1, 14],
    'rc4_final_key': [255],
    # Byte prefix prepended before the RC4'd 19-byte body.
    'prefix': [2, 255],
    # Checksum seed (initial XOR value).
    'checksum_seed': 64,
    # Salt list trailer byte.
    'trailer': 255,
}


def rc4_encrypt(data: bytes, key: bytes) -> bytes:
    """RC4 stream cipher over bytes (KSA + PRGA, XOR keystream)."""
    s_box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s_box[i] + key[i % len(key)]) % 256
        s_box[i], s_box[j] = s_box[j], s_box[i]

    out = bytearray(len(data))
    t = 0
    j = 0
    for i, byte in enumerate(data):
        t = (t + 1) % 256
        j = (j + s_box[t]) % 256
        s_box[t], s_box[j] = s_box[j], s_box[t]
        out[i] = byte ^ s_box[(s_box[t] + s_box[j]) % 256]
    return bytes(out)


def md5_double(data: bytes) -> bytes:
    """Double MD5: md5(md5(data).digest()).digest()."""
    return hashlib.md5(hashlib.md5(data).digest()).digest()


def custom_b64_encode(data: bytes, charset: str) -> str:
    """Base64 bit-packing with the X-Bogus alphabet (no '=' padding needed
    for the fixed 21-byte input; handles the tail exactly like standard
    base64 would with '=' chars, using index 64 fallback — but the alphabet
    has 64 chars so callers must keep input length a multiple of 3)."""
    table = charset
    out = []
    for i in range(0, len(data) - len(data) % 3, 3):
        b0, b1, b2 = data[i], data[i + 1], data[i + 2]
        out.append(table[b0 >> 2])
        out.append(table[((b0 & 3) << 4) | (b1 >> 4)])
        out.append(table[((b1 & 15) << 2) | (b2 >> 6)])
        out.append(table[b2 & 63])
    return ''.join(out)


class TikTokSigner:
    """Generates X-Bogus signatures for TikTok web API requests.

    Usage:
        signer = TikTokSigner(user_agent=UA)
        signed_url = signer.sign_url(url)          # appends &X-Bogus=...
        xb = signer.generate_x_bogus(url)          # raw 28-char value

    Replacement / reconfiguration:
        TikTokSigner(sign_func=my_browser_signer)  # whole-algorithm swap
        signer.update_config(magic=0x12345678)     # constant drift patch
    """

    def __init__(self, user_agent: str = None,
                 config: dict = None,
                 sign_func: Callable[[str, str], str] = None):
        """Args:
            user_agent: default UA bound into every signature (X-Bogus is
                UA-bound; the request MUST use the same UA).
            config: partial overrides of DEFAULT_CONFIG (charset, magic, ...).
            sign_func: optional full replacement: sign_func(url, ua) -> str
                returning the X-Bogus value. Takes precedence over the
                built-in algorithm.
        """
        self.user_agent = user_agent or (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        )
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.sign_func = sign_func

    # ------------------------------------------------------------------
    # Reconfiguration (page JS parameter drift support)
    # ------------------------------------------------------------------
    def update_config(self, **overrides) -> None:
        """Patch algorithm constants at runtime (e.g. after recalibrating
        against a new webmssdk build)."""
        self.config.update(overrides)

    # ------------------------------------------------------------------
    # Signature
    # ------------------------------------------------------------------
    def generate_x_bogus(self, url: str, user_agent: str = None,
                         timestamp: int = None) -> str:
        """Return the 28-char X-Bogus value for `url` (query is extracted).

        Args:
            url: full URL or bare query string (must NOT already contain
                an X-Bogus parameter).
            user_agent: defaults to self.user_agent.
            timestamp: override for deterministic tests / replay.
        """
        if self.sign_func is not None:
            return self.sign_func(url, user_agent or self.user_agent)

        ua = user_agent or self.user_agent
        query = self._extract_query(url)
        ts = int(timestamp if timestamp is not None else time.time())
        return self._x_bogus(query, ua, ts)

    def sign_url(self, url: str, user_agent: str = None) -> str:
        """Append `&X-Bogus=...` to the URL and return it."""
        xb = self.generate_x_bogus(url, user_agent)
        sep = '&' if '?' in url else '?'
        return f"{url}{sep}X-Bogus={xb}"

    def headers_for(self, url: str, user_agent: str = None) -> dict:
        """Convenience: request headers carrying UA + X-Bogus.

        The query-embedded X-Bogus is the canonical placement (public
        implementations append it to the URL); the header copy is a
        belt-and-suspenders addition — placement header vs query is an
        ASSUMPTION pending on-device verification.
        """
        ua = user_agent or self.user_agent
        xb = self.generate_x_bogus(url, ua)
        return {'User-Agent': ua, 'X-Bogus': xb}

    # ------------------------------------------------------------------
    # Core algorithm (public-domain reproduction, see module docstring)
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_query(url: str) -> str:
        """Accept a full URL or a bare query string."""
        if '://' in url:
            return urlparse(url).query
        if url.startswith('?'):
            return url[1:]
        return url

    def _x_bogus(self, query: str, user_agent: str, timestamp: int) -> str:
        cfg = self.config
        magic = int(cfg['magic'])

        md5_params = md5_double(query.encode())
        md5_data = md5_double(b'')  # GET: empty body
        ua_b64 = base64.b64encode(
            rc4_encrypt(user_agent.encode(), bytes(cfg['rc4_ua_key']))
        )
        md5_ua = hashlib.md5(ua_b64).digest()

        salt_list = [
            timestamp, magic,
            64, 0, 1, 14,
            md5_params[-2], md5_params[-1],
            md5_data[-2], md5_data[-1],
            md5_ua[-2], md5_ua[-1],
        ]
        salt_list.extend([(timestamp >> i) & 0xFF for i in range(24, -1, -8)])
        salt_list.extend([(magic >> i) & 0xFF for i in range(24, -1, -8)])

        checksum = cfg['checksum_seed']
        for value in salt_list[3:]:
            checksum ^= value
        salt_list.extend([checksum, cfg['trailer']])

        # filter: interleave-odd/even pick of salt_list[2:21] (19 items)
        pick = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20,
                3, 5, 7, 9, 11, 13, 15, 17, 19]
        num_list = [salt_list[i] for i in pick]

        # scramble: a,k,b,l,c,m,d,n,e,o,f,p,g,q,h,r,i,s,j
        order = [0, 10, 1, 11, 2, 12, 3, 13, 4, 14, 5, 15, 6, 16, 7, 17, 8, 18, 9]
        scrambled = bytes(num_list[i] for i in order)

        rc4_out = rc4_encrypt(scrambled, bytes(cfg['rc4_final_key']))
        payload = bytes(cfg['prefix']) + rc4_out  # 2 + 19 = 21 bytes

        return custom_b64_encode(payload, cfg['charset'])
