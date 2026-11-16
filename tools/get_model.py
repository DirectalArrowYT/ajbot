"""Install the newest shared model from the GitHub releases.

    python tools/get_model.py            # install it if it is newer than yours
    python tools/get_model.py --check    # only say what is out there
    python tools/get_model.py --force    # reinstall it anyway

Models are published with training/release_model.bat. The one you have now
is kept in models/replaced-<time>/ (pick it as Bot B to compare the two).
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import json
import time
from pathlib import Path

import doctor
from ajbot import modelpack


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", action="store_true", help="with --check: one JSON line (GUI)")
    a = ap.parse_args()

    have = modelpack.installed()
    rel, asset = doctor.release_with(modelpack.PREFIX)
    tag = rel.get("tag_name")
    info = {"installed": have.get("tag"), "latest": tag,
            "size_mb": round(asset["size"] / 1e6), "published": rel.get("published_at"),
            "notes": (rel.get("body") or "").split("\n\n")[0].strip(),
            "local_policy": Path("models/policy.pkl").exists()}
    if a.check:
        if a.json:
            print(json.dumps(info))
            return
        print(f"newest shared model: {tag} ({info['size_mb']} MB, {info['published']})")
        if info["notes"]:
            print("  " + info["notes"].replace("\n", "\n  "))
        print(f"yours: {have.get('tag') or ('a model of your own' if info['local_policy'] else 'none')}")
        return
    if have.get("tag") == tag and not a.force:
        print(f"you already have the newest shared model ({tag})")
        return
    cache = Path("cache")
    cache.mkdir(exist_ok=True)
    zpath = cache / asset["name"]
    print(f"downloading {asset['name']} ({info['size_mb']} MB)")
    doctor.download(asset["browser_download_url"], zpath)
    t0 = time.time()
    modelpack.install(zpath, tag)
    zpath.unlink(missing_ok=True)
    print(f"done in {time.time() - t0:.0f} s - Start a match to use it")


if __name__ == "__main__":
    main()
