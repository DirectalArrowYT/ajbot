"""Imported first by every script in this folder: puts the repo root and the
script folders (bot/, server/, training/, tools/) on sys.path, so `import ajbot`
and cross-folder imports like `import train` work from anywhere.

It does NOT change directory: these scripts use the working directory as
their data root (league runs train.py inside the corpus folder), so start
them from the AJBot folder, or from the corpus, as before."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("", "bot", "server", "training", "tools"):
    _p = os.path.join(ROOT, _d) if _d else ROOT
    if _p not in sys.path:
        sys.path.insert(1, _p)
