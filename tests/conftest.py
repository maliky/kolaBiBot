"""Pytest configuration ensuring in-place imports and quiet deps."""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Silence noisy third-party deprecation warnings (websockets/binance).
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r".*websockets\.WebSocketClientProtocol.*",
)
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r".*websockets\.legacy.*",
)
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r"There is no current event loop",
    module=r"binance\.helpers",
)
