"""Bring this copy up to date: code, the GUI and the game-side files.

    python tools/update.py           # update everything that is out of date
    python tools/update.py --check   # only say what would change

  * code: `git pull` when this folder is a git checkout
  * GUI: ajbot-gui.exe from the latest release when its tag is newer than the
    one installed here (the running exe is renamed to .old and replaced; the
    new one is used from the next start)
  * game files: tools/doctor.py --fix gamefiles when the release is newer
    than the one installed in the game (skipped while the game is running)
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor  # noqa: E402

GUI = Path("ajbot-gui.exe")
STAMP = Path("cache/release.json")      # {"gui": "<tag>"} of what is installed here


def git_pull(check):
    if not Path(".git").exists():
        print("code: not a git checkout - download the new source from GitHub to update it")
        return
    if check:
        subprocess.call(["git", "fetch", "-q"])
        out = subprocess.run(["git", "status", "-sb"], capture_output=True, text=True).stdout
        print("code: " + (out.splitlines()[0] if out else "?"))
        return
    print("code: git pull", flush=True)
    if subprocess.call(["git", "pull", "--ff-only"]) != 0:
        print("!! git pull failed - local changes in the way? (git status shows them)")


def load_stamp():
    try:
        return json.loads(STAMP.read_text())
    except (OSError, ValueError):
        return {}


def update_gui(rel, check):
    tag = rel.get("tag_name")
    asset = next((a for a in rel.get("assets", []) if a["name"] == GUI.name), None)
    stamp = load_stamp()
    tracked = subprocess.run(["git", "ls-files", GUI.name], capture_output=True,
                             text=True).stdout.strip() if Path(".git").exists() else ""
    if tracked:
        print("gui: committed in this checkout - git pull / cargo build updates it")
        return
    if asset is None:
        print(f"gui: release {tag} has no {GUI.name}")
        return
    if stamp.get("gui") == tag and GUI.exists():
        print(f"gui: up to date ({tag})")
        return
    if check:
        print(f"gui: {stamp.get('gui') or 'unknown'} -> {tag}")
        return
    new = GUI.with_suffix(".exe.new")
    print(f"gui: downloading {tag}", flush=True)
    doctor.download(asset["browser_download_url"], new)
    if GUI.exists():
        old = GUI.with_suffix(".exe.old")
        try:
            old.unlink()
        except OSError:
            pass
        os.replace(GUI, old)            # Windows allows renaming a running exe
    os.replace(new, GUI)
    STAMP.parent.mkdir(exist_ok=True)
    STAMP.write_text(json.dumps({**stamp, "gui": tag}))
    print("gui: updated - restart the GUI to use it")


def update_gamefiles(rel, check):
    tag = rel.get("tag_name")
    game = doctor.find_game()
    if game is None:
        print("game files: game folder not set (Setup page)")
        return
    win64 = doctor.win64_of(game)
    try:
        have = json.loads((win64 / doctor.GAMEFILES_STAMP).read_text()).get("version")
    except (OSError, ValueError):
        have = None
    if have == tag:
        print(f"game files: up to date ({tag})")
        return
    if check:
        print(f"game files: {have or 'not from a release'} -> {tag}")
        return
    if doctor.game_running():
        print("game files: close the game to update them")
        return
    doctor.install_gamefiles(win64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    git_pull(a.check)
    try:
        rel = doctor.latest_release()
    except SystemExit as e:
        print(f"releases: {e}")
        return
    except Exception as e:
        print(f"releases: could not reach GitHub ({e})")
        return
    print(f"latest release: {rel.get('tag_name')}")
    update_gui(rel, a.check)
    update_gamefiles(rel, a.check)


if __name__ == "__main__":
    main()
