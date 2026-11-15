"""Run a Lua chunk on the game's Unreal side through the AJProbe UE4SS mod.

    python ueprobe.py 'return FindFirstOf("GameEngine"):GetFullName()'
    python ueprobe.py -f probe.lua          # chunk from a file
    python ueprobe.py --dump                # UE4SS object dump (DumpAllObjects)
    python ueprobe.py --sdk                 # UE4SS C++ header dump (GenerateSDK)

Inside the chunk, out(...) adds a line to the result. Set AJ_GAME_DIR if the
game is not in the default Steam library. Dumps land in the game's Win64
folder (UE4SS_ObjectDump.txt, CXXHeaderDump/).
"""
import os
import sys
import time
from pathlib import Path

CANDIDATES = [
    os.environ.get("AJ_GAME_DIR", ""),
    __import__("ajbot.local", fromlist=["get"]).get("game.dir", ""),
    "C:/Program Files (x86)/Steam/steamapps/common/MY HERO ACADEMIA All's Justice",
    "D:/SteamLibrary/steamapps/common/MY HERO ACADEMIA All's Justice",
]


def mod_dir():
    for g in CANDIDATES:
        d = Path(g) / "HeroGame/Binaries/Win64/ue4ss/Mods/AJProbe"
        if g and d.is_dir():
            return d
    sys.exit("AJProbe mod not found (set AJ_GAME_DIR)")


def probe(src, timeout=600.0):
    d = mod_dir()
    inst = os.environ.get("AJ_INSTANCE", "")
    sfx = f"-{inst}" if inst not in ("", "0") else ""   # see AJProbe main.lua
    cmd, out = d / f"cmd{sfx}.lua", d / f"out{sfx}.txt"
    out.unlink(missing_ok=True)
    tmp = d / f"cmd{sfx}.lua.tmp"
    tmp.write_text(src, encoding="utf-8")
    tmp.replace(cmd)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if out.exists() and not cmd.exists():
            return out.read_text(encoding="utf-8", errors="replace")
        time.sleep(0.1)
    raise TimeoutError("no answer from AJProbe (is the game running with the mod?)")


def main(argv):
    if not argv:
        sys.exit(__doc__)
    if argv[0] == "--dump":
        src = "DumpAllObjects() return 'dumped'"
    elif argv[0] == "--sdk":
        src = "GenerateSDK() return 'generated'"
    elif argv[0] == "-f":
        src = Path(argv[1]).read_text(encoding="utf-8")
    else:
        src = " ".join(argv)
    sys.stdout.write(probe(src))


if __name__ == "__main__":
    main(sys.argv[1:])
