import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import sys
from ajbot.combobook import main

if __name__ == "__main__":
    sys.exit(main())
