import sys
from pathlib import Path

# Make `hil` importable when pytest is run from the repo root.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
