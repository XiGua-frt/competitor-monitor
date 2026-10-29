"""Unit tests for fingerprint pool manager."""

import pytest
import json
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from src.pools.fingerprint_pool import (
    FingerprintPoolManager,
    DEVICE_TEMPLATES,
    TIMEZONE_OPTIONS,
    LOCALE_OPTIONS
)
from src.pools.models import BrowserFingerprint


class TestFingerprintPoolManager:
    """Test cases for FingerprintPoolManager."""
    
    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis client."""
        redis_mock = Mock()
        redis_mock.get.return_value = None
        redis_mock.keys.return_value = []
        redis_mock.set.return_value = True
        redis_mock.expire.return_value = True
        redis_mock.delete.return_value = 1
        return redis_mock
    
    @pytest.fixture
    def manager(self, mock_redis):
        """Create FingerprintPoolManager instance."""
        return FingerprintPoolManager(redis_client=mock_redis)
    
    def test_initialization(self, manager):
        """Test manager initialization."""
        assert manager is not None
        assert isinstance(manager.fingerprints, dict)
        assert len(manager.fingerprints) == 0
    
    def test_add_fingerprint_default(self, manager):
        """Test adding fingerprint with default device."""
        account_id = "test_account_001"
        
        fingerprint = manager.add_fingerprint(account_id)
        
        assert fingerprint is not None
        assert fingerprint.fingerprint_id == f"fp_{account_id}"
        assert fingerprint.user_agent != ""
        assert fingerprint.canvas_noise != ""
        assert fingerprint.timezone in TIMEZONE_OPTIONS
        assert fingerprint.locale in LOCALE_OPTIONS
        
        # Verify in memory cache
        assert f"fp_{account_id}" in manager.fingerprints
    
    def test_add_fingerprint_specific_device(self, manager):
        """Test adding fingerprint with specific device type."""
        account_id = "test_account_002"
        device_type = "Windows 10 Pro (RTX 3080)"
        
        fingerprint = manager.add_fingerprint(account_id, device_type=device_type)
        
        assert fingerprint is not None
        assert "Windows NT 10.0" in fingerprint.user_agent
        assert fingerprint.platform == "Win32"
        assert fingerprint.viewport == {'width': 2560, 'height': 1440}
    
    def test_canvas_noise_uniqueness(self, manager):
        """Test that canvas noise is unique for each fingerprint."""
        canvas_noises = set()
        
        for i in range(10):
            account_id = f"test_account_{i:03d}"
            fingerprint = manager.add_fingerprint(account_id)
            canvas_noises.add(fingerprint.canvas_noise)
        
        # All 10 canvas noises should be unique
        assert len(canvas_noises) == 10
    
    def test_canvas_hash_generation(self, manager):
        """Test canvas hash generation."""
        account_id = "test_account_hash"
        fingerprint = manager.add_fingerprint(account_id)
        
        canvas_hash = manager.get_canvas_hash(fingerprint.canvas_noise)
        
        assert canvas_hash is not None
        assert len(canvas_hash) == 64  # SHA256 hex digest length
    
    def test_get_fingerprint_existing(self, manager):
        """Test getting existing fingerprint."""
        account_id = "test_account_get"
        
        # Add fingerprint
        original = manager.add_fingerprint(account_id)
        
        # Get fingerprint
        retrieved = manager.get_fingerprint(account_id)
        
        assert retrieved is not None
        assert retrieved.fingerprint_id == original.fingerprint_id
        assert retrieved.canvas_noise == original.canvas_noise
    
    def test_get_fingerprint_auto_create(self, manager):
        """Test auto-creating fingerprint when not found."""
        account_id = "test_account_new"
        
        # Get non-existent fingerprint (should auto-create)
        fingerprint = manager.get_fingerprint(account_id)
        
        assert fingerprint is not None
        assert fingerprint.fingerprint_id == f"fp_{account_id}"
    
    def test_remove_fingerprint(self, manager, mock_redis):
        """Test removing fingerprint."""
        account_id = "test_account_remove"
        
        # Add fingerprint
        manager.add_fingerprint(account_id)
        
        # Remove fingerprint
        result = manager.remove_fingerprint(account_id)
        
        assert result is True
        assert f"fp_{account_id}" not in manager.fingerprints
        mock_redis.delete.assert_called()
    
    def test_get_context_options(self, manager):
        """Test getting Playwright context options."""
        account_id = "test_account_context"
        fingerprint = manager.add_fingerprint(account_id)
        
        options = manager.get_context_options(fingerprint)
        
        assert 'viewport' in options
        assert 'screen' in options
        assert 'user_agent' in options
        assert 'locale' in options
        assert 'timezone_id' in options
        assert options['user_agent'] == fingerprint.user_agent
        assert options['locale'] == fingerprint.locale
    
    def test_apply_to_browser(self, manager):
        """Test applying fingerprint to browser context (mock)."""
        account_id = "test_account_apply"
        fingerprint = manager.add_fingerprint(account_id)
        
        # Mock browser context
        mock_context = Mock()
        mock_context.add_init_script = Mock()
        
        # Apply fingerprint
        manager.apply_to_browser(mock_context, fingerprint)
        
        # Verify init script was added
        mock_context.add_init_script.assert_called_once()
        
        # Get the script that was injected
        call_args = mock_context.add_init_script.call_args[0][0]
        assert fingerprint.canvas_noise in call_args
        assert fingerprint.webgl_vendor in call_args
        assert fingerprint.webgl_renderer in call_args
    
    def test_device_template_coverage(self, manager):
        """Test all device templates are accessible."""
        device_types = [t['name'] for t in DEVICE_TEMPLATES]
        
        assert len(device_types) == 5
        assert 'MacBook Pro M1 (Ventura)' in device_types
        assert 'MacBook Air M2 (Sonoma)' in device_types
        assert 'Windows 10 Pro (RTX 3080)' in device_types
        assert 'Windows 11 Home (GTX 1660)' in device_types
        assert 'iPad Pro 12.9 (iPadOS 17)' in device_types
    
    def test_statistics(self, manager):
        """Test getting statistics."""
        # Add multiple fingerprints
        for i in range(5):
            manager.add_fingerprint(f"account_{i}")
        
        stats = manager.get_statistics()
        
        assert stats['total_fingerprints'] == 5
        assert 'device_distribution' in stats
        assert 'timezone_distribution' in stats
        assert 'locale_distribution' in stats
        assert stats['available_templates'] == 5
    
    def test_redis_persistence(self, manager, mock_redis):
        """Test Redis save/load operations."""
        account_id = "test_account_redis"
        
        # Add fingerprint
        fingerprint = manager.add_fingerprint(account_id)
        
        # Verify Redis set was called
        mock_redis.set.assert_called()
        mock_redis.expire.assert_called_with(
            f"fingerprint:fp_{account_id}",
            90 * 24 * 60 * 60
        )
        
        # Get call arguments
        call_args = mock_redis.set.call_args[0]
        assert call_args[0] == f"fingerprint:fp_{account_id}"
        
        # Verify JSON serialization
        saved_data = json.loads(call_args[1])
        assert saved_data['fingerprint_id'] == f"fp_{account_id}"
        assert saved_data['canvas_noise'] == fingerprint.canvas_noise
    
    def test_multiple_accounts_different_fingerprints(self, manager):
        """Test that multiple accounts get different fingerprints."""
        fingerprints = []
        
        for i in range(10):
            account_id = f"account_{i:03d}"
            fp = manager.add_fingerprint(account_id)
            fingerprints.append(fp)
        
        # All fingerprint IDs should be unique
        fingerprint_ids = [fp.fingerprint_id for fp in fingerprints]
        assert len(set(fingerprint_ids)) == 10
        
        # All canvas noises should be unique
        canvas_noises = [fp.canvas_noise for fp in fingerprints]
        assert len(set(canvas_noises)) == 10
    
    def test_ipad_specific_options(self, manager):
        """Test iPad-specific context options."""
        account_id = "test_ipad"
        device_type = "iPad Pro 12.9 (iPadOS 17)"
        
        fingerprint = manager.add_fingerprint(account_id, device_type=device_type)
        options = manager.get_context_options(fingerprint)
        
        assert options['has_touch'] is True
        assert options['is_mobile'] is True
        assert fingerprint.platform == 'iPad'
    
    def test_desktop_specific_options(self, manager):
        """Test desktop-specific context options."""
        account_id = "test_desktop"
        device_type = "MacBook Pro M1 (Ventura)"
        
        fingerprint = manager.add_fingerprint(account_id, device_type=device_type)
        options = manager.get_context_options(fingerprint)
        
        assert options['has_touch'] is False
        assert options['is_mobile'] is False
        assert fingerprint.platform == 'MacIntel'


class TestFingerprintUniqueness:
    """Test fingerprint uniqueness across multiple instances."""
    
    def test_canvas_noise_collision_resistance(self):
        """Test canvas noise generation collision resistance."""
        mock_redis = Mock()
        mock_redis.get.return_value = None
        mock_redis.keys.return_value = []
        
        manager = FingerprintPoolManager(redis_client=mock_redis)
        
        # Generate 100 fingerprints
        canvas_hashes = set()
        for i in range(100):
            fp = manager.add_fingerprint(f"account_{i}")
            canvas_hash = manager.get_canvas_hash(fp.canvas_noise)
            canvas_hashes.add(canvas_hash)
        
        # All should be unique
        assert len(canvas_hashes) == 100


def test_device_templates_structure():
    """Test device template data structure."""
    required_fields = [
        'name', 'user_agent', 'viewport', 'screen_resolution',
        'webgl_vendor', 'webgl_renderer', 'platform',
        'cpu_cores', 'device_memory', 'hardware_concurrency'
    ]
    
    for template in DEVICE_TEMPLATES:
        for field in required_fields:
            assert field in template, f"Missing field '{field}' in template '{template.get('name')}'"
        
        # Validate viewport structure
        assert 'width' in template['viewport']
        assert 'height' in template['viewport']
        assert template['viewport']['width'] > 0
        assert template['viewport']['height'] > 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
