# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import sys
import os
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("MAILINTEL_CONFIG_DIR", tempfile.mkdtemp(prefix="mailintel-test-config-"))
os.environ.setdefault("MAILINTEL_API_TOKEN", "test-token")
