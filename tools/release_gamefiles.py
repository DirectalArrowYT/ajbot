"""Publish the game-side files as a GitHub release, for everyone's Setup page.

    tools\\release_gamefiles.bat                 # build, then ask before publishing
    tools\\release_gamefiles.bat --no-build      # reuse the last cargo builds
    python tools/release_gamefiles.py --dry-run  # build the zip only

Runs tools/make_release.py (ASI loader + UE4SS from this PC's game install,
this repo's UE4SS mods, a fresh ajhook.asi; it refuses if any private string
turns up) and publishes dist/ajbot-gamefiles-<ver>.zip as release
gamefiles-<ver>. Setup > Install game files and Update now fetch the newest
one. Only the newest --keep game-file releases are kept.

Login: see ajbot/ghrelease.py (GH_TOKEN, else the login `git push` uses).
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import subprocess
import zipfile
from pathlib import Path

from ajbot import ghrelease as gh

PREFIX = "ajbot-gamefiles-"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-build", action="store_true", help="reuse the last cargo builds")
    ap.add_argument("--game", help="game folder to take the loader/UE4SS from")
    ap.add_argument("--notes", default="", help="a line about this release")
    ap.add_argument("--keep", type=int, default=3, help="game-file releases to keep (default 3)")
    ap.add_argument("--yes", action="store_true", help="do not ask before publishing")
    ap.add_argument("--dry-run", action="store_true", help="build the zip, publish nothing")
    a = ap.parse_args()

    args = [sys.executable, "tools/make_release.py"]
    if a.no_build:
        args.append("--no-build")
    if a.game:
        args += ["--game", a.game]
    if subprocess.call(args) != 0:
        raise SystemExit("make_release.py failed - nothing published")
    zips = sorted(Path("dist").glob(f"{PREFIX}*.zip"), key=lambda p: p.stat().st_mtime)
    if not zips:
        raise SystemExit("no dist/ajbot-gamefiles-*.zip was built")
    zpath = zips[-1]
    ver = zpath.stem[len(PREFIX):]
    tag = f"gamefiles-{ver}"
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
    # Top-level files and folders, each UE4SS mod on its own line.
    top = sorted({"/".join(n.split("/")[:3 if n.startswith("ue4ss/Mods/") else 1]) for n in names})
    lines = [a.notes] if a.notes else []
    lines += ["Game-side files for AJBot: unzip into `HeroGame/Binaries/Win64` "
              "(Setup > **Install game files** does it for you).", "",
              "- `dinput8.dll`: Ultimate ASI Loader",
              "- `dwmapi.dll`, `ue4ss/`: UE4SS (HookLoadMap = 0) with its stock mods",
              "- `ue4ss/Mods/CursorExport`, `AJProbe`: the bot's UE4SS mods",
              "- `ajhook.asi`: the bot's in-game hook", "",
              "Update: Setup > **Update now** (or `python tools/update.py`)."]
    body = "\n".join(lines)
    print(f"\n{zpath} ({zpath.stat().st_size / 1e6:.1f} MB, {len(names)} files) as {tag}")
    for t in top:
        print(f"    {t}")
    if a.dry_run:
        print("\n--- release notes ---\n" + body + "\n--- dry run: nothing published ---")
        return
    if not a.yes and not gh.confirm(zpath.name):
        print("not published (the zip stays in dist/)")
        return
    tok = gh.token()
    print(f"published: {gh.publish(tok, tag, f'Game files {ver}', body, zpath)}")
    gh.prune(tok, "gamefiles-", PREFIX, a.keep, "game-file release")


if __name__ == "__main__":
    main()
