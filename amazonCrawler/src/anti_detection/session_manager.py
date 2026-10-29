"""Session manager: persist Amazon cookie dict to data/amz_session.json.

契约签名：load / save / clear / has_session（存储 cookie dict）。
"""

import json
from pathlib import Path
from typing import Dict, Optional

try:  # pragma: no cover - 取决于 Agent A 是否已就绪
    from src.common.logger import get_logger

    _COMMON_READY = True
except ImportError:
    from . import _common_fallback as _fb  # type: ignore

    get_logger = _fb.get_logger
    _COMMON_READY = False

logger = get_logger(__name__)

DEFAULT_SESSION_FILE = Path('data/amz_session.json')


class SessionManager:
    """Amazon session cookie persistence (JSON file)."""

    def __init__(self, session_file: Optional[Path] = None):
        """Initialize session manager.

        Args:
            session_file: 存储路径；默认 data/amz_session.json（相对项目根）
        """
        self.session_file = Path(session_file) if session_file else DEFAULT_SESSION_FILE
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self._cookies: Dict[str, str] = {}

    def has_session(self) -> bool:
        """内存或文件里存在非空 cookie 即视为有会话。"""
        if self._cookies:
            return True
        return self._read_file() is not None

    def load(self) -> Optional[Dict[str, str]]:
        """加载 cookie dict（优先内存缓存，其次文件）。

        Returns:
            cookie dict；无会话或文件损坏返回 None
        """
        if self._cookies:
            return dict(self._cookies)

        data = self._read_file()
        if data is None:
            return None
        self._cookies = data
        logger.info(f"Loaded session with {len(data)} cookies from {self.session_file}")
        return dict(self._cookies)

    def save(self, cookies: Dict[str, str]) -> bool:
        """保存 cookie dict 到文件并更新内存缓存。

        Returns:
            True 保存成功
        """
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                'cookies': dict(cookies),
                'updated_at': __import__('datetime').datetime.now().isoformat(),
            }
            self.session_file.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8'
            )
            self._cookies = dict(cookies)
            logger.info(f"Saved session with {len(cookies)} cookies to {self.session_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to save session: {e}")
            return False

    def clear(self) -> None:
        """清空内存与文件中的会话。"""
        self._cookies = {}
        try:
            self.session_file.unlink(missing_ok=True)
        except Exception as e:  # pragma: no cover - 文件系统异常防御
            logger.warning(f"Failed to remove session file: {e}")
        logger.info("Session cleared")

    def _read_file(self) -> Optional[Dict[str, str]]:
        """读取文件并解析 cookie dict；不存在/损坏返回 None。"""
        if not self.session_file.exists():
            return None
        try:
            payload = json.loads(self.session_file.read_text(encoding='utf-8'))
            cookies = payload.get('cookies') if isinstance(payload, dict) else None
            if isinstance(cookies, dict):
                return {str(k): str(v) for k, v in cookies.items()}
            return None
        except (ValueError, OSError) as e:
            logger.warning(f"Corrupted session file {self.session_file}: {e}")
            return None
