"""Publish the model in models/ as a GitHub release, for everyone's GUI.

    training\\release_model.bat                     # asks before publishing
    training\\release_model.bat --notes "beats gen 258 7-3"
    python training/release_model.py --dry-run      # only build the zip

Packs models/ (ajbot/modelpack.py) into dist/ajbot-model-<tag>.zip and
publishes it as release <tag> of the public repo, not marked "latest" (the
GUI's own builds keep that). Users press Get latest model on the Train page
(tools/get_model.py) to install it. Only the newest --keep model releases
are kept.

Login: see ajbot/ghrelease.py (GH_TOKEN, else the login `git push` uses).
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import json
import time
from pathlib import Path

from ajbot import modelpack
from ajbot import ghrelease as gh

def last_comparison():
    """The latest 'N matches, then compare' verdict for this policy, if any."""
    p = Path("stats/compare.jsonl")
    if not p.exists():
        return ""
    try:
        e = json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ""
    if Path(e.get("a", "")).name != "policy.pkl":
        return ""
    return f"Last local comparison ({e.get('matches')} matches vs {Path(e['b']).stem}): {e.get('verdict')}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--notes", default="", help="a line about this model for the release page")
    ap.add_argument("--keep", type=int, default=5, help="model releases to keep (default 5)")
    ap.add_argument("--yes", action="store_true", help="do not ask before publishing")
    ap.add_argument("--dry-run", action="store_true", help="build the zip, publish nothing")
    a = ap.parse_args()

    tag = time.strftime("model-%Y.%m.%d-%H%M")
    print(f"packing models/ as {tag} ...")
    zpath, man = modelpack.pack(Path("dist"), tag, a.notes)
    mb = zpath.stat().st_size / 1e6
    print(f"  {zpath} ({mb:.0f} MB, {len(man['files'])} files)")
    for f, m in man["files"].items():
        print(f"    {f:<22} {m['size'] / 1e6:8.1f} MB  {m['sha256'][:12]}")
    gen = man.get("league_gen")
    lines = [a.notes] if a.notes else []
    if gen is not None:
        lines.append(f"League champion gen {gen}.")
    cmp_line = last_comparison()
    if cmp_line:
        lines.append(cmp_line)
    lines += ["", "Install: GUI > Train > **Get latest model** (or `python tools/get_model.py`).",
              "Your current model is kept in models/replaced-<time>/ and can be picked as Bot B.",
              "", "Files: " + ", ".join(man["files"])]
    body = "\n".join(lines)
    if a.dry_run:
        print("\n--- release notes ---\n" + body + "\n--- dry run: nothing published ---")
        return
    if not a.yes and not gh.confirm(zpath.name):
        print("not published (the zip stays in dist/)")
        return
    tok = gh.token()
    url = gh.publish(tok, tag, f"Model {tag[6:]}" + (f" (gen {gen})" if gen is not None else ""),
                     body, zpath)
    print(f"published: {url}")
    gh.prune(tok, "model-", modelpack.PREFIX, a.keep, "model release")


if __name__ == "__main__":
    main()
