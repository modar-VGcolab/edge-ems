"""Put the ext-ems package root on sys.path so `import ext_ems` works when
pytest is run from anywhere (the package lives beside this file)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
