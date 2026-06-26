"""
conftest.py

Makes k8s_agent/ importable from tests/ without needing to install the
package or manually adjust sys.path in every test file.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "k8s_agent"))
