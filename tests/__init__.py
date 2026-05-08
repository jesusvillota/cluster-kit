# Tests for cluster-kit

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC_PATH = Path(__file__).resolve().parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

os.environ.setdefault("CLUSTER_REMOTE_BASE", "/tmp/cluster-kit-tests")
os.environ.setdefault("CLUSTER_USER", "j-vill36")
