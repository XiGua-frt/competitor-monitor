"""Chrome → TikTok 会话材料提取工具（macOS）。

用法：
    /path/to/python tools/extract_tiktok_session.py

前提：
- macOS + Google Chrome，且用户已在 Chrome 里登录过 tiktok.com；
- 钥匙串可访问 "Chrome Safe Storage"（首次会弹授权框，点"允许"）。

产出：.secrets/tiktok_session.json（chmod 600，含全部 .tiktok.com cookie）。
安全：不打印任何 cookie 值；产物不要提交到任何仓库。

关键实现细节（Chrome 130+，2026-09 实测 v153）：
- cookie 加密 = v10 前缀 + AES-128-CBC（key=PBKDF2-HMAC-SHA1(钥匙串密码,
  salt='saltysalt', 1003 轮, 16B), IV=16 个空格）；
- DB meta.version >= 24 时，明文前 32 字节是 SHA256(domain)，解密后必须去掉。
"""

import json
import os
import sqlite3
import stat
import subprocess
import tempfile
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

CHROME_COOKIES = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Default/Cookies")
OUT_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".secrets", "tiktok_session.json")

WANT = ["sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt",
        "ttwid", "msToken", "odin_tt", "tt-target-idc", "s_v_web_id",
        "tt_chain_token", "multi_sids", "sid_ucp_v1"]


def chrome_time_to_iso(ts):
    if not ts:
        return None
    epoch = ts / 1_000_000 - 11644473600
    return (datetime.fromtimestamp(epoch, timezone.utc).isoformat()
            if epoch > 0 else None)


def get_key() -> bytes:
    pw = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
        capture_output=True, text=True, check=True).stdout.strip()
    return PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt",
                      iterations=1003).derive(pw.encode())


def decrypt(blob: bytes, key: bytes) -> str | None:
    if not blob:
        return None
    if blob[:3] not in (b"v10", b"v11"):
        return blob.decode("utf-8", errors="replace")
    d = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    plain = d.update(blob[3:]) + d.finalize()
    pad = plain[-1]
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    # Chrome 130+ (meta.version>=24)：明文前 32 字节为 SHA256(domain)
    if len(plain) > 32:
        plain = plain[32:]
    try:
        val = plain.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    return val if val.isascii() else None


def main() -> None:
    key = get_key()
    # Chrome 运行时会锁库，先复制再读
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
    subprocess.run(["cp", CHROME_COOKIES, tmp_path], check=True)
    conn = sqlite3.connect(tmp_path)
    rows = conn.execute(
        "SELECT host_key, name, encrypted_value, value, expires_utc "
        "FROM cookies WHERE host_key LIKE '%tiktok%' ORDER BY host_key"
    ).fetchall()
    conn.close()
    os.unlink(tmp_path)

    cookies, meta = {}, {}
    for host, name, enc, plain, exp in rows:
        val = plain or decrypt(enc, key)
        if not val:
            continue
        if host in (".tiktok.com", "www.tiktok.com", ".tiktokw.us"):
            cookies.setdefault(name, val)
            meta[name] = {"host": host, "expires": chrome_time_to_iso(exp)}

    print("== 提取结果（仅名称与长度）==")
    for w in WANT:
        v = cookies.get(w)
        print(f"  {w}: " + (f"OK len={len(v)} exp={meta[w]['expires']}"
                            if v else "MISSING"))

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump({
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "profile": "Chrome/Default",
            "cookies": cookies,
        }, f, ensure_ascii=False, indent=1)
    os.chmod(OUT_FILE, stat.S_IRUSR | stat.S_IWUSR)
    print(f"\nsaved: {OUT_FILE} (chmod 600) cookies={len(cookies)}")
    print("logged_in:",
          bool(cookies.get("sessionid") or cookies.get("sid_tt")
               or cookies.get("multi_sids")))


if __name__ == "__main__":
    main()
