"""Pre-match setup: pick a 3-character team, then set every slot to Manual.

    python setup_match.py                          # default team
    python setup_match.py Eraserhead Bakugo Dabi
    python setup_match.py --plan                   # show the route, press nothing
    python setup_match.py --names                  # list characters in grid order
    python setup_match.py --control-only           # just the control-mode screen

Grid geometry is derived from the game's own DT_VsCharSelect ordering and was
verified against the live screen, so picks should be exact. The one assumption
left is where the cursor starts (--start, default the top-left cell).
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import sys
import time

from ajbot import Pad, menus, grid as G
from ajbot.roster import DISPLAY_ORDER, INDEX

DEFAULT_TEAM = ["Eraserhead", "Bakugo", "Dabi"]
COUNTDOWN = 5


def countdown(msg):
    print(msg)
    for t in range(COUNTDOWN, 0, -1):
        print(f"  starting in {t}...", end="\r", flush=True)
        time.sleep(1)
    print(" " * 44, end="\r")


def main():
    args = sys.argv[1:]
    flags = [a for a in args if a.startswith("-")]
    team = [a for a in args if not a.startswith("-")] or DEFAULT_TEAM

    start = "DekuFinal"
    if "--start" in args:
        start = args[args.index("--start") + 1]
        team = [t for t in team if t != start] or DEFAULT_TEAM

    if "--names" in flags:
        for i, cid in enumerate(DISPLAY_ORDER):
            r, c = divmod(i, G.COLS)
            print(f"{i:>3}  row {r:>2} col {c}  {cid}")
        return

    if "--control-only" in flags:
        pad = Pad()
        countdown("focus the game on the control-mode screen")
        try:
            menus.set_all_manual(pad, count=3)
            pad.idle(20)
        finally:
            pad.reset()
            print("done - pad released")
        return

    unknown = [c for c in team if c not in INDEX]
    if unknown:
        print(f"unknown character(s): {', '.join(unknown)}  (try --names)")
        return
    if len(team) != 3:
        print(f"need exactly 3 characters, got {len(team)}")
        return

    planning = "--plan" in flags
    pad = None if planning else Pad()
    nav = menus.CharacterSelect(pad or _NullPad(), start=start)

    print(f"grid {G.ROWS}x{G.COLS}, cursor starts on {start}, side {nav.side}")
    for name in team:
        print("  " + nav.describe(name))
        nav.cursor = G.position_of(name)
    if planning:
        return

    nav.cursor = G.position_of(start)
    countdown("focus the game on character select")
    try:
        nav.select_team(team)
        print("team locked in; control-mode screen next")
        pad.idle(menus.SETTLE * 2)
        menus.set_all_manual(pad, count=3)
        pad.idle(20)
    finally:
        pad.reset()
        print("done - pad released")


class _NullPad:
    frame = 0
    def tap(self, *a, **k): pass
    def tap_n(self, *a, **k): pass
    def idle(self, *a, **k): pass
    def reset(self): pass


if __name__ == "__main__":
    main()
