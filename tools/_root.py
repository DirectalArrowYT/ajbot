"""Imported first by every script in tools/: puts the repo root on sys.path
(so `import ajbot` works) and makes it the working directory (so the
relative models/, runs/, stats/ paths these scripts use resolve)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)
