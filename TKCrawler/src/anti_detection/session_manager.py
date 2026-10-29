"""Session and cookie management for maintaining TikTok login state."""

import json
import time
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timedelta

from ..common.config import config
from ..common.logger import get_logger
from ..common.exceptions import SessionExpiredException

logger = get_logger(__name__)


class SessionManager:
    """Manages TikTok session cookies and login state."""
    
    def __init__(self, session_file: Path = None):
        """Initialize session manager.
        
        Args:
            session_file: Path to store session cookies
        """
        self.session_file = session_file or Path('data/session.json')
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        
        self._cookies: Dict[str, str] = {}
        self._last_refresh: Optional[datetime] = None
        self._expiry_time: timedelta = timedelta(hours=24)
    
    def load_session(self) -> bool:
        """Load session from file.
        
        Returns:
            True if session loaded successfully
        """
        if not self.session_file.exists():
            logger.warning("Session file not found")
            return False
        
        try:
            with open(self.session_file, 'r') as f:
                data = json.load(f)
                self._cookies = data.get('cookies', {})
                self._last_refresh = datetime.fromisoformat(data.get('last_refresh', ''))
            
            logger.info(f"Loaded session with {len(self._cookies)} cookies")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load session: {e}")
            return False
    
    def save_session(self) -> bool:
        """Save session to file.
        
        Returns:
            True if saved successfully
        """
        try:
            data = {
                'cookies': self._cookies,
                'last_refresh': datetime.now().isoformat()
            }
            
            with open(self.session_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.info("Session saved successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save session: {e}")
            return False
    
    def get_cookies(self) -> Dict[str, str]:
        """Get current cookies.
        
        Returns:
            Dictionary of cookies
        
        Raises:
            SessionExpiredException: When session is expired
        """
        if not self._cookies:
            if not self.load_session():
                raise SessionExpiredException("No valid session found")
        
        if self.is_expired():
            raise SessionExpiredException("Session expired")
        
        return self._cookies
    
    def set_cookies(self, cookies: Dict[str, str]):
        """Set cookies and save to file.
        
        Args:
            cookies: Dictionary of cookies
        """
        self._cookies = cookies
        self._last_refresh = datetime.now()
        self.save_session()
        
        logger.info(f"Updated session with {len(cookies)} cookies")
    
    def is_expired(self) -> bool:
        """Check if session is expired.
        
        Returns:
            True if expired
        """
        if not self._last_refresh:
            return True
        
        elapsed = datetime.now() - self._last_refresh
        return elapsed > self._expiry_time
    
    def refresh_session(self) -> bool:
        """Refresh session by re-logging in.
        
        Returns:
            True if refresh successful
        """
        logger.info("Refreshing session...")
        
        try:
            # Import here to avoid circular dependency
            from .browser_client import get_tiktok_cookies
            
            cookies = get_tiktok_cookies()
            self.set_cookies(cookies)
            
            logger.info("Session refreshed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to refresh session: {e}")
            return False
    
    def is_alive(self) -> bool:
        """Check if session is alive by making a test request.
        
        Returns:
            True if session is alive
        """
        try:
            cookies = self.get_cookies()
            
            # Import here to avoid circular dependency
            from .curl_client import CurlCffiClient
            
            client = CurlCffiClient()
            
            # Test request to TikTok API
            headers = {
                'Cookie': '; '.join([f'{k}={v}' for k, v in cookies.items()])
            }
            
            response = client.get(
                'https://www.tiktok.com/passport/web/account/info/?aid=1988',
                headers=headers
            )

            # Check if response indicates logged in.
            # NOTE: verified 2026-10-08 — /api/user/info/ requires signature params
            # (returns "url doesn't match"), passport account/info works with bare
            # sessionid cookies. TikTok returns snake_case `status_code`, and the
            # logged-in payload nests account info under `data.user_id`.
            if response.status_code == 200:
                data = response.json()
                if data.get('data', {}).get('user_id'):
                    logger.info(
                        f"Session is alive (user_id={data['data']['user_id']})"
                    )
                    return True
            
            logger.warning(f"Session check failed: {response.status_code}")
            return False
            
        except Exception as e:
            logger.error(f"Session check error: {e}")
            return False
    
    def clear_session(self):
        """Clear current session."""
        self._cookies = {}
        self._last_refresh = None
        
        if self.session_file.exists():
            self.session_file.unlink()
        
        logger.info("Session cleared")


# Global session manager instance
session_manager = SessionManager()
