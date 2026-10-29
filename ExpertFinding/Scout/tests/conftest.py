"""Shared pytest bootstrap for the Scout test-suite.

The project has no packaging metadata, so the repository root is put on
``sys.path`` explicitly. This keeps ``import app.domain...`` working no matter
which directory pytest is invoked from.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
