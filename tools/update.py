"""Bring this copy up to date: code, the GUI and the game-side files.

    python tools/update.py           # update everything that is out of date
    python tools/update.py --check   # only say what would change

  * code: `git pull` when this folder is a git checkout
  * GUI: ajbot-gui.exe from the newest release that has one (GitHub Actions
    publishes one per GUI change) when it differs from the one installed here (the running exe is renamed to .old and replaced; the
    new one is used from the next start)
  * game files: tools/doctor.py --fix gamefiles when the newest release with
    an ajbot-gamefiles zip differs from the one installed in the game (skipped while the game is running)
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import json
import os
import shutil
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


def update_gui(check):
    try:
        rel, asset = doctor.release_with(doctor.GUI_ASSET)
    except SystemExit as e:
        print(f"gui: {e}")
        return
    tag = rel.get("tag_name")
    stamp = load_stamp()
    tracked = subprocess.run(["git", "ls-files", GUI.name], capture_output=True,
                             text=True).stdout.strip() if Path(".git").exists() else ""
    if tracked:
        # A dev checkout: the exe comes from this PC's own build, not Releases.
        built = Path("gui/target/release") / GUI.name
        if built.exists() and (not GUI.exists() or built.stat().st_mtime > GUI.stat().st_mtime):
            if check:
                print("gui: your local build (gui/target/release) is newer - update copies it in")
                return
            try:
                # A running exe can be renamed but not overwritten.
                old = GUI.with_suffix(".exe.old")
                old.unlink(missing_ok=True)
                if GUI.exists():
                    GUI.rename(old)
                shutil.copy2(built, GUI)
                print("gui: copied in your newer local build - restart the GUI to use it")
            except OSError as e:
                print(f"gui: could not copy the local build ({e})")
        else:
            print("gui: committed in this checkout - cargo build in gui/, then update copies it in")
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


def update_gamefiles(check):
    try:
        rel, _ = doctor.release_with(doctor.GAMEFILES_PREFIX)
    except SystemExit as e:
        print(f"game files: {e}")
        return
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
        update_gui(a.check)
        update_gamefiles(a.check)
    except OSError as e:                     # urllib errors included
        print(f"releases: could not reach GitHub ({e})")


if __name__ == "__main__":
    main()
