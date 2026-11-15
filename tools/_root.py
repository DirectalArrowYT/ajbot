"""Imported first by every script in tools/:
puts the repo root and the script folders on sys.path (so `import ajbot` and
cross-folder imports like `import train` work) and makes the repo root the
working directory (so relative models/, runs/, stats/ paths resolve)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("", "bot", "server", "training", "tools"):
    _p = os.path.join(ROOT, _d) if _d else ROOT
    if _p not in sys.path:
        sys.path.insert(1, _p)
os.chdir(ROOT)
