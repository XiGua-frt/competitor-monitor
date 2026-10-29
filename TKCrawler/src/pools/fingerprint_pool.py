"""Fingerprint Pool Manager for browser fingerprinting."""

import os
import base64
import random
import hashlib
from typing import Dict, Optional
from datetime import datetime, timedelta
import redis
import json

from .models import BrowserFingerprint
from ..common.logger import get_logger
from ..common.config import config

logger = get_logger(__name__)


# Device templates - 5 common configurations
DEVICE_TEMPLATES = [
    {
        'name': 'MacBook Pro M1 (Ventura)',
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'viewport': {'width': 1920, 'height': 1080},
        'screen_resolution': '1920x1080',
        'webgl_vendor': 'Google Inc. (Apple)',
        'webgl_renderer': 'ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)',
        'platform': 'MacIntel',
        'cpu_cores': 8,
        'device_memory': 16,
        'hardware_concurrency': 8,
    },
    {
        'name': 'MacBook Air M2 (Sonoma)',
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'viewport': {'width': 1440, 'height': 900},
        'screen_resolution': '1440x900',
        'webgl_vendor': 'Google Inc. (Apple)',
        'webgl_renderer': 'ANGLE (Apple, Apple M2, OpenGL 4.1)',
        'platform': 'MacIntel',
        'cpu_cores': 8,
        'device_memory': 8,
        'hardware_concurrency': 8,
    },
    {
        'name': 'Windows 10 Pro (RTX 3080)',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'viewport': {'width': 2560, 'height': 1440},
        'screen_resolution': '2560x1440',
        'webgl_vendor': 'Google Inc. (NVIDIA)',
        'webgl_renderer': 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)',
        'platform': 'Win32',
        'cpu_cores': 16,
        'device_memory': 32,
        'hardware_concurrency': 16,
    },
    {
        'name': 'Windows 11 Home (GTX 1660)',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'viewport': {'width': 1920, 'height': 1080},
        'screen_resolution': '1920x1080',
        'webgl_vendor': 'Google Inc. (NVIDIA)',
        'webgl_renderer': 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)',
        'platform': 'Win32',
        'cpu_cores': 12,
        'device_memory': 16,
        'hardware_concurrency': 12,
    },
    {
        'name': 'iPad Pro 12.9 (iPadOS 17)',
        'user_agent': 'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
        'viewport': {'width': 2732, 'height': 2048},
        'screen_resolution': '2732x2048',
        'webgl_vendor': 'Apple Inc.',
        'webgl_renderer': 'Apple GPU',
        'platform': 'iPad',
        'cpu_cores': 8,
        'device_memory': 8,
        'hardware_concurrency': 8,
    },
]

# Timezone options
TIMEZONE_OPTIONS = [
    'America/Los_Angeles',
    'America/New_York',
    'America/Chicago',
    'Europe/London',
    'Europe/Paris',
    'Asia/Tokyo',
    'Asia/Shanghai',
    'Asia/Singapore',
]

# Locale options
LOCALE_OPTIONS = [
    'en-US',
    'en-GB',
    'fr-FR',
    'de-DE',
    'ja-JP',
    'zh-CN',
    'ko-KR',
]


class FingerprintPoolManager:
    """Manages browser fingerprints for anti-detection."""
    
    def __init__(self, redis_client: redis.Redis = None):
        """Initialize fingerprint pool manager.
        
        Args:
            redis_client: Redis client for persistence
        """
        if redis_client is None:
            redis_client = redis.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                password=config.REDIS_PASSWORD,
                db=config.REDIS_DB,
                decode_responses=True
            )
        
        self.redis = redis_client
        self.fingerprints: Dict[str, BrowserFingerprint] = {}
        
        # Load existing fingerprints from Redis
        self._load_all_fingerprints()
        
        logger.info(f"FingerprintPoolManager initialized with {len(self.fingerprints)} fingerprints")
    
    def add_fingerprint(
        self,
        account_id: str,
        device_type: Optional[str] = None
    ) -> BrowserFingerprint:
        """Add or regenerate fingerprint for an account.
        
        Args:
            account_id: Account ID
            device_type: Specific device template name (optional)
        
        Returns:
            BrowserFingerprint object
        """
        fingerprint_id = f"fp_{account_id}"
        
        # Select device template
        if device_type:
            template = next(
                (t for t in DEVICE_TEMPLATES if t['name'] == device_type),
                random.choice(DEVICE_TEMPLATES)
            )
        else:
            template = random.choice(DEVICE_TEMPLATES)
        
        # Generate unique canvas noise
        canvas_noise = self._generate_canvas_noise()
        
        # Random timezone and locale
        timezone = random.choice(TIMEZONE_OPTIONS)
        locale = random.choice(LOCALE_OPTIONS)
        
        fingerprint = BrowserFingerprint(
            fingerprint_id=fingerprint_id,
            user_agent=template['user_agent'],
            viewport=template['viewport'],
            screen_resolution=template['screen_resolution'],
            webgl_vendor=template['webgl_vendor'],
            webgl_renderer=template['webgl_renderer'],
            platform=template['platform'],
            cpu_cores=template['cpu_cores'],
            device_memory=template['device_memory'],
            hardware_concurrency=template['hardware_concurrency'],
            timezone=timezone,
            locale=locale,
            canvas_noise=canvas_noise
        )
        
        # Save to Redis with 90-day TTL
        self._save_fingerprint(fingerprint)
        
        # Cache in memory
        self.fingerprints[fingerprint_id] = fingerprint
        
        logger.info(
            f"Generated fingerprint {fingerprint_id} "
            f"(device: {template['name']}, timezone: {timezone}, locale: {locale})"
        )
        
        return fingerprint
    
    def get_fingerprint(self, account_id: str) -> Optional[BrowserFingerprint]:
        """Get fingerprint for an account.
        
        Args:
            account_id: Account ID
        
        Returns:
            BrowserFingerprint object or None if not found
        """
        fingerprint_id = f"fp_{account_id}"
        
        # Check memory cache first
        if fingerprint_id in self.fingerprints:
            return self.fingerprints[fingerprint_id]
        
        # Try to load from Redis
        fingerprint = self._load_fingerprint(fingerprint_id)
        if fingerprint:
            self.fingerprints[fingerprint_id] = fingerprint
            return fingerprint
        
        # Generate new fingerprint if not found
        logger.warning(f"Fingerprint not found for {account_id}, generating new one")
        return self.add_fingerprint(account_id)
    
    def remove_fingerprint(self, account_id: str) -> bool:
        """Remove fingerprint for an account.
        
        Args:
            account_id: Account ID
        
        Returns:
            True if removed successfully
        """
        fingerprint_id = f"fp_{account_id}"
        
        # Remove from Redis
        self.redis.delete(f"fingerprint:{fingerprint_id}")
        
        # Remove from memory
        if fingerprint_id in self.fingerprints:
            del self.fingerprints[fingerprint_id]
            logger.info(f"Removed fingerprint {fingerprint_id}")
            return True
        
        return False
    
    def apply_to_browser(
        self,
        context,
        fingerprint: BrowserFingerprint
    ):
        """Apply fingerprint to Playwright browser context.
        
        Args:
            context: Playwright browser context
            fingerprint: BrowserFingerprint object
        """
        try:
            # Note: Most fingerprint parameters are applied during context creation
            # This method is for runtime modifications if needed
            
            # Inject canvas noise script
            context.add_init_script(f"""
                // Override canvas fingerprinting
                const canvasNoise = '{fingerprint.canvas_noise}';
                
                const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
                HTMLCanvasElement.prototype.toDataURL = function() {{
                    const context = this.getContext('2d');
                    if (context) {{
                        // Add noise to canvas
                        const imageData = context.getImageData(0, 0, this.width, this.height);
                        const noise = atob(canvasNoise);
                        for (let i = 0; i < imageData.data.length; i++) {{
                            if (i < noise.length) {{
                                imageData.data[i] ^= noise.charCodeAt(i % noise.length);
                            }}
                        }}
                        context.putImageData(imageData, 0, 0);
                    }}
                    return originalToDataURL.apply(this, arguments);
                }};
                
                // Override WebGL fingerprinting
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {{
                    if (parameter === 37445) {{
                        return '{fingerprint.webgl_vendor}';
                    }}
                    if (parameter === 37446) {{
                        return '{fingerprint.webgl_renderer}';
                    }}
                    return getParameter.apply(this, arguments);
                }};
            """)
            
            logger.debug(f"Applied fingerprint {fingerprint.fingerprint_id} to browser context")
            
        except Exception as e:
            logger.error(f"Failed to apply fingerprint: {e}")
            raise
    
    def get_context_options(self, fingerprint: BrowserFingerprint) -> dict:
        """Get Playwright context options for a fingerprint.
        
        Args:
            fingerprint: BrowserFingerprint object
        
        Returns:
            Context options dictionary
        """
        screen_width = int(fingerprint.screen_resolution.split('x')[0])
        screen_height = int(fingerprint.screen_resolution.split('x')[1])
        
        options = {
            'viewport': fingerprint.viewport,
            'screen': {
                'width': screen_width,
                'height': screen_height
            },
            'user_agent': fingerprint.user_agent,
            'locale': fingerprint.locale,
            'timezone_id': fingerprint.timezone,
            'device_scale_factor': 2 if 'Retina' in fingerprint.screen_resolution or 'iPad' in fingerprint.platform else 1,
            'has_touch': fingerprint.platform == 'iPad',
            'is_mobile': fingerprint.platform == 'iPad',
        }
        
        return options
    
    def _generate_canvas_noise(self) -> str:
        """Generate unique canvas noise for fingerprinting.
        
        Returns:
            Base64 encoded noise
        """
        # Generate 32 random bytes with timestamp for uniqueness
        timestamp = str(datetime.now().timestamp()).encode()
        random_bytes = os.urandom(32)
        noise = hashlib.sha256(timestamp + random_bytes).digest()
        return base64.b64encode(noise).decode('utf-8')
    
    def get_canvas_hash(self, canvas_noise: str) -> str:
        """Get hash of canvas noise for verification.
        
        Args:
            canvas_noise: Base64 encoded canvas noise
        
        Returns:
            SHA256 hash of canvas noise
        """
        return hashlib.sha256(canvas_noise.encode()).hexdigest()
    
    def _save_fingerprint(self, fingerprint: BrowserFingerprint):
        """Save fingerprint to Redis.
        
        Args:
            fingerprint: BrowserFingerprint object
        """
        key = f"fingerprint:{fingerprint.fingerprint_id}"
        data = fingerprint.to_dict()
        
        # Convert to JSON string
        self.redis.set(key, json.dumps(data))
        
        # Set TTL to 90 days
        self.redis.expire(key, 90 * 24 * 60 * 60)
        
        logger.debug(f"Saved fingerprint {fingerprint.fingerprint_id} to Redis")
    
    def _load_fingerprint(self, fingerprint_id: str) -> Optional[BrowserFingerprint]:
        """Load fingerprint from Redis.
        
        Args:
            fingerprint_id: Fingerprint ID
        
        Returns:
            BrowserFingerprint object or None
        """
        key = f"fingerprint:{fingerprint_id}"
        data = self.redis.get(key)
        
        if not data:
            return None
        
        try:
            data_dict = json.loads(data)
            return BrowserFingerprint.from_dict(data_dict)
        except Exception as e:
            logger.error(f"Failed to load fingerprint {fingerprint_id}: {e}")
            return None
    
    def _load_all_fingerprints(self):
        """Load all fingerprints from Redis on startup."""
        try:
            keys = self.redis.keys("fingerprint:*")
            loaded_count = 0
            
            for key in keys:
                fingerprint_id = key.split(":", 1)[1]
                fingerprint = self._load_fingerprint(fingerprint_id)
                if fingerprint:
                    self.fingerprints[fingerprint_id] = fingerprint
                    loaded_count += 1
            
            logger.info(f"Loaded {loaded_count} fingerprints from Redis")
        except Exception as e:
            logger.error(f"Failed to load fingerprints from Redis: {e}")
    
    def get_all_fingerprints(self) -> Dict[str, BrowserFingerprint]:
        """Get all fingerprints.
        
        Returns:
            Dictionary of fingerprint_id -> BrowserFingerprint
        """
        return self.fingerprints.copy()
    
    def get_statistics(self) -> dict:
        """Get fingerprint pool statistics.
        
        Returns:
            Statistics dictionary
        """
        device_counts = {}
        timezone_counts = {}
        locale_counts = {}
        
        for fp in self.fingerprints.values():
            # Count by platform
            platform = fp.platform
            device_counts[platform] = device_counts.get(platform, 0) + 1
            
            # Count by timezone
            timezone_counts[fp.timezone] = timezone_counts.get(fp.timezone, 0) + 1
            
            # Count by locale
            locale_counts[fp.locale] = locale_counts.get(fp.locale, 0) + 1
        
        return {
            'total_fingerprints': len(self.fingerprints),
            'device_distribution': device_counts,
            'timezone_distribution': timezone_counts,
            'locale_distribution': locale_counts,
            'available_templates': len(DEVICE_TEMPLATES),
        }
