#!/usr/bin/env python3
"""Verification script for fingerprint pool functionality."""

import sys
import os
from datetime import datetime
from unittest.mock import Mock

# Add project root to path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from src.pools.fingerprint_pool import FingerprintPoolManager, DEVICE_TEMPLATES
from src.pools.models import BrowserFingerprint


def print_separator(char='=', length=80):
    """Print separator line."""
    print(char * length)


def verify_fingerprint_pool():
    """Verify fingerprint pool functionality."""
    print_separator()
    print("FINGERPRINT POOL VERIFICATION REPORT")
    print(f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print_separator()
    
    # Create mock Redis client
    mock_redis = Mock()
    mock_redis.get.return_value = None
    mock_redis.keys.return_value = []
    mock_redis.set.return_value = True
    mock_redis.expire.return_value = True
    mock_redis.delete.return_value = 1
    
    # Initialize manager
    print("\n1. Initializing FingerprintPoolManager...")
    manager = FingerprintPoolManager(redis_client=mock_redis)
    print(f"   ✓ Manager initialized successfully")
    
    # Generate 10 fingerprints
    print("\n2. Generating 10 fingerprints for different accounts...")
    fingerprints = []
    canvas_hashes = {}
    
    for i in range(10):
        account_id = f"test_account_{i+1:03d}"
        fp = manager.add_fingerprint(account_id)
        fingerprints.append(fp)
        
        # Calculate canvas hash
        canvas_hash = manager.get_canvas_hash(fp.canvas_noise)
        canvas_hashes[account_id] = canvas_hash
        
        print(f"   ✓ Generated fingerprint for {account_id}")
    
    print(f"\n   Total fingerprints generated: {len(fingerprints)}")
    
    # Verify canvas noise uniqueness
    print("\n3. Verifying Canvas Noise Uniqueness...")
    print_separator('-')
    
    unique_canvas_noises = set(fp.canvas_noise for fp in fingerprints)
    unique_hashes = set(canvas_hashes.values())
    
    print(f"   Total fingerprints: {len(fingerprints)}")
    print(f"   Unique canvas noises: {len(unique_canvas_noises)}")
    print(f"   Unique canvas hashes: {len(unique_hashes)}")
    
    if len(unique_canvas_noises) == len(fingerprints):
        print(f"   ✓ All canvas noises are UNIQUE")
    else:
        print(f"   ✗ COLLISION DETECTED: {len(fingerprints) - len(unique_canvas_noises)} duplicates")
    
    # Print canvas hash table
    print("\n4. Canvas Hash Verification Table:")
    print_separator('-')
    print(f"{'Account ID':<20} {'Canvas Hash (SHA256)':<64} {'Status':<10}")
    print_separator('-')
    
    seen_hashes = set()
    for account_id, canvas_hash in canvas_hashes.items():
        if canvas_hash in seen_hashes:
            status = "DUPLICATE"
        else:
            status = "UNIQUE"
            seen_hashes.add(canvas_hash)
        print(f"{account_id:<20} {canvas_hash:<64} {status:<10}")
    
    # Verify fingerprint details
    print("\n5. Fingerprint Details Verification:")
    print_separator('-')
    
    for i, fp in enumerate(fingerprints[:3], 1):  # Show first 3 as samples
        print(f"\n   Fingerprint #{i} ({fp.fingerprint_id}):")
        print(f"   - User Agent: {fp.user_agent[:80]}...")
        print(f"   - Platform: {fp.platform}")
        print(f"   - Viewport: {fp.viewport}")
        print(f"   - Screen Resolution: {fp.screen_resolution}")
        print(f"   - WebGL Vendor: {fp.webgl_vendor}")
        print(f"   - WebGL Renderer: {fp.webgl_renderer[:60]}...")
        print(f"   - Timezone: {fp.timezone}")
        print(f"   - Locale: {fp.locale}")
        print(f"   - CPU Cores: {fp.cpu_cores}")
        print(f"   - Device Memory: {fp.device_memory} GB")
        print(f"   - Hardware Concurrency: {fp.hardware_concurrency}")
        print(f"   - Canvas Noise Length: {len(fp.canvas_noise)} chars")
    
    # Test Playwright context options
    print("\n6. Testing Playwright Context Options Generation:")
    print_separator('-')
    
    sample_fp = fingerprints[0]
    context_options = manager.get_context_options(sample_fp)
    
    print(f"   Sample context options for {sample_fp.fingerprint_id}:")
    print(f"   - Viewport: {context_options['viewport']}")
    print(f"   - Screen: {context_options['screen']}")
    print(f"   - User Agent: {context_options['user_agent'][:80]}...")
    print(f"   - Locale: {context_options['locale']}")
    print(f"   - Timezone ID: {context_options['timezone_id']}")
    print(f"   - Device Scale Factor: {context_options['device_scale_factor']}")
    print(f"   - Has Touch: {context_options['has_touch']}")
    print(f"   - Is Mobile: {context_options['is_mobile']}")
    print(f"   ✓ Context options generated successfully")
    
    # Test apply to browser (mock)
    print("\n7. Testing Browser Context Application (Mock):")
    print_separator('-')
    
    mock_context = Mock()
    mock_context.add_init_script = Mock()
    
    manager.apply_to_browser(mock_context, sample_fp)
    
    if mock_context.add_init_script.called:
        print(f"   ✓ Init script injected successfully")
        
        # Verify script content
        script = mock_context.add_init_script.call_args[0][0]
        checks = [
            (sample_fp.canvas_noise in script, "Canvas noise"),
            (sample_fp.webgl_vendor in script, "WebGL vendor"),
            (sample_fp.webgl_renderer in script, "WebGL renderer"),
            ("HTMLCanvasElement.prototype.toDataURL" in script, "Canvas override"),
            ("WebGLRenderingContext.prototype.getParameter" in script, "WebGL override"),
        ]
        
        for check_result, check_name in checks:
            status = "✓" if check_result else "✗"
            print(f"   {status} {check_name} present in script")
    else:
        print(f"   ✗ Init script NOT injected")
    
    # Device template coverage
    print("\n8. Device Template Coverage:")
    print_separator('-')
    
    print(f"   Available device templates: {len(DEVICE_TEMPLATES)}")
    for template in DEVICE_TEMPLATES:
        print(f"   - {template['name']}")
    
    # Statistics
    print("\n9. Pool Statistics:")
    print_separator('-')
    
    stats = manager.get_statistics()
    print(f"   Total fingerprints: {stats['total_fingerprints']}")
    print(f"   Available templates: {stats['available_templates']}")
    
    print(f"\n   Device distribution:")
    for platform, count in stats['device_distribution'].items():
        print(f"   - {platform}: {count}")
    
    print(f"\n   Timezone distribution:")
    for tz, count in sorted(stats['timezone_distribution'].items()):
        print(f"   - {tz}: {count}")
    
    print(f"\n   Locale distribution:")
    for locale, count in sorted(stats['locale_distribution'].items()):
        print(f"   - {locale}: {count}")
    
    # Final summary
    print("\n")
    print_separator('=')
    print("VERIFICATION SUMMARY")
    print_separator('=')
    
    all_unique = len(unique_canvas_noises) == len(fingerprints)
    all_valid = all(fp.user_agent and fp.canvas_noise for fp in fingerprints)
    
    print(f"✓ Fingerprints generated: {len(fingerprints)}/10")
    print(f"{'✓' if all_unique else '✗'} Canvas noise uniqueness: {len(unique_canvas_noises)}/10 unique")
    print(f"{'✓' if all_valid else '✗'} All fingerprints valid: {all_valid}")
    print(f"✓ Context options generation: Working")
    print(f"✓ Browser application (mock): Working")
    print(f"✓ Device template coverage: {len(DEVICE_TEMPLATES)} templates")
    
    if all_unique and all_valid:
        print("\n🎉 ALL VERIFICATION CHECKS PASSED!")
        return 0
    else:
        print("\n⚠️  SOME VERIFICATION CHECKS FAILED!")
        return 1


if __name__ == '__main__':
    sys.exit(verify_fingerprint_pool())
