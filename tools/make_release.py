"""Build the files for a GitHub release: the GUI and the game-side bundle.

    python tools/make_release.py              # builds into dist/
    python tools/make_release.py --no-build   # reuse the last cargo builds

Writes:
    dist/ajbot-gui.exe                     the GUI
    dist/ajbot-gamefiles-<version>.zip     unzips into HeroGame/Binaries/Win64:
        dsound.dll                         Ultimate ASI Loader (AJGAME.exe imports DSOUND.dll;
                                           nothing loads dinput8.dll on many Windows PCs)
        dwmapi.dll, ue4ss/                 UE4SS core + its stock mods,
                                           HookLoadMap = 0 in the settings
        ue4ss/Mods/CursorExport, AJProbe   this repo's ue4ss_mods/
        ajhook.asi                         freshly built from ajhook/

The loader and UE4SS are taken from this machine's working game install.
Upload both files to a new release on GitHub; tools/doctor.py --fix gamefiles
installs the zip on other machines. Trained models go up separately
(models.zip) when you choose to share them.
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor  # noqa: E402
try:
    import export_public  # noqa: E402  (not in the public repo; the scan is skipped there)
except ImportError:
    export_public = None

DIST = Path("dist")
# Stock UE4SS mods kept in the bundle (BPModLoader needs the generic functions;
# Keybinds and shared are part of UE4SS itself).
STOCK_MODS = ("BPModLoaderMod", "BPML_GenericFunctions", "Keybinds", "shared")
THIRD_PARTY = {"dsound.dll", "dwmapi.dll", "ue4ss/UE4SS.dll"}
SKIP_SUFFIX = (".log", ".dmp", ".old", ".bak", ".txt.bak")


# Off Windows the release is cross-built; on Windows the host target is Windows already.
WIN_TARGET = "" if os.name == "nt" else "x86_64-pc-windows-gnu"


def cargo(folder):
    """Release build with this machine's paths (user name, repo folder) remapped
    out of panic messages, in its own target dir so the normal build cache stays."""
    env = dict(os.environ)
    remaps = [(Path.home(), "~"), (Path.cwd(), "ajbot")]
    flags = env.pop("RUSTFLAGS", "").split()
    flags += [f"--remap-path-prefix={src}={dst}" for src, dst in remaps]
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)   # survives spaces in paths
    env["CARGO_TARGET_DIR"] = str(Path(folder, "target", "dist").resolve())
    cmd = ["cargo", "build", "--release"]
    if WIN_TARGET:   # cross-build the Windows .dll / .exe (needs mingw-w64-gcc)
        cmd += ["--target", WIN_TARGET]
    print(f"> {' '.join(cmd)}   ({folder}, paths remapped)", flush=True)
    subprocess.check_call(cmd, cwd=folder, env=env)
    return Path(env["CARGO_TARGET_DIR"], WIN_TARGET, "release")


def private_hits(blobs):
    """(name, string) for every private string found in the shipped bytes."""
    if export_public is None:
        return []
    bad = [b.encode() for b in export_public.private_strings()]
    bad += [b.replace(b"/", b"\\") for b in bad if b"/" in b]
    # Bare drive roots ("X:/Users") are on export_public's list as generic
    # catch-alls; third-party builds carry their CI's own user folders under
    # them, so there only this machine's specific strings count.
    generic = {b for b in bad if len(b) <= 12 and b[1:2] == b":"}
    hits = []
    for name, data in blobs:
        third = name in THIRD_PARTY
        hits += [(name, b.decode()) for b in set(bad)
                 if b in data and not (third and b in generic)]
    return hits


def version():
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        sha = "nogit"
    return f"{datetime.date.today():%Y.%m.%d}-{sha}"


def keep(path):
    n = path.name
    return not (n.endswith(SKIP_SUFFIX) or ".bak" in n or n.startswith("crash_")
                or n == "UE4SS_ObjectDump.txt")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--game", help="game folder to take the loader/UE4SS from")
    a = ap.parse_args()

    game = Path(a.game) if a.game else doctor.find_game()
    if game is None:
        raise SystemExit("game folder not found - pass --game")
    win64 = doctor.win64_of(game)
    hook = Path("ajhook/target/dist", WIN_TARGET, "release/ajhook.dll")
    gui = Path("gui/target/dist", WIN_TARGET, "release/ajbot-gui.exe")
    if not a.no_build:
        hook = cargo("ajhook") / "ajhook.dll"
        gui = cargo("gui") / "ajbot-gui.exe"
    # The loader ships as dsound.dll whatever this install calls it (same Ultimate ASI Loader binary).
    loader = next((win64 / n for n in ("dsound.dll", "dinput8.dll") if (win64 / n).exists()), win64 / "dsound.dll")
    for p in (hook, gui, loader, win64 / "dwmapi.dll", win64 / "ue4ss" / "UE4SS.dll"):
        if not p.exists():
            raise SystemExit(f"missing {p}")

    ver = version()
    DIST.mkdir(exist_ok=True)
    for old in DIST.glob("ajbot-gamefiles-*.zip"):
        old.unlink()
    zpath = DIST / f"ajbot-gamefiles-{ver}.zip"
    ue = win64 / "ue4ss"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(loader, "dsound.dll")
        z.write(win64 / "dwmapi.dll", "dwmapi.dll")
        z.write(hook, "ajhook.asi")
        z.write(ue / "UE4SS.dll", "ue4ss/UE4SS.dll")
        if (ue / "LICENSE").exists():
            z.write(ue / "LICENSE", "ue4ss/LICENSE")
        ini = (ue / "UE4SS-settings.ini").read_text(errors="ignore")
        ini = re.sub(r"^(\s*HookLoadMap\s*=\s*)\d+", r"\g<1>0", ini, flags=re.M)
        z.writestr("ue4ss/UE4SS-settings.ini", ini)
        for m in STOCK_MODS:
            for f in sorted((ue / "Mods" / m).rglob("*")):
                if f.is_file() and keep(f):
                    z.write(f, f"ue4ss/Mods/{m}/{f.relative_to(ue / 'Mods' / m).as_posix()}")
        for m in doctor.OUR_MODS:
            src = Path("ue4ss_mods") / m
            for f in sorted(src.rglob("*")):
                if f.is_file() and keep(f):
                    z.write(f, f"ue4ss/Mods/{m}/{f.relative_to(src).as_posix()}")
        enabled = ["BPML_GenericFunctions", "BPModLoaderMod", *doctor.OUR_MODS]
        z.writestr("ue4ss/Mods/mods.txt",
                   "".join(f"{m} : 1\n" for m in enabled)
                   + "\n; Built-in keybinds, do not move up!\nKeybinds : 1\n")
        z.writestr(doctor.GAMEFILES_STAMP, json.dumps({"version": ver}))
        n = len(z.namelist())
    with zipfile.ZipFile(zpath) as z:
        blobs = [(i, z.read(i)) for i in z.namelist()]
    hits = private_hits(blobs + [(gui.name, gui.read_bytes())])
    if hits:
        zpath.unlink()
        for name, s in hits:
            print(f"  private string {s!r} in {name}")
        raise SystemExit("not releasing: private strings above (zip deleted)")
    shutil.copy2(gui, DIST / gui.name)
    print(f"\n{zpath}  ({zpath.stat().st_size // 1024} KB, {n} files)")
    print(f"{DIST / gui.name}  ({gui.stat().st_size // 1024} KB)")
    print("(tools\\release_gamefiles.bat publishes the zip; the GUI exe is published by "
          "GitHub Actions on every gui/ change)")


if __name__ == "__main__":
    main()
