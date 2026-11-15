"""Show which XInput slots are occupied right now, and watch for changes.

Point of this: the 8BitDo adapter (through Steam Input) sometimes registers
one physical controller as TWO XInput slots (commonly slots 0 and 1, i.e.
"Player 1 and Player 2"). When that happens, the next virtual pad play.py or
play2.py creates lands on the NEXT free slot - Player 3 - instead of Player 2,
and the match setup breaks in ways that are confusing to debug live. This
script just watches the raw slot state, with no game or bot involved, so you
can confirm the adapter is back to one slot BEFORE launching a match.

    python check_pads.py            # one snapshot
    python check_pads.py --watch    # keep printing on every change (Ctrl+C to stop)
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import time

from ajbot.pad import SLOT_NAMES, _xinput_connected


def snapshot():
    slots = sorted(_xinput_connected())
    names = ", ".join(f"{s} ({SLOT_NAMES.get(s, '?')})" for s in slots)
    return slots, f"{len(slots)} occupied: {names}" if slots else "0 occupied"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="keep polling until Ctrl+C")
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()

    slots, line = snapshot()
    print(line)
    if len(slots) > 1:
        print("  !! more than one slot occupied with no bot pad running yet - "
              "if this is one physical controller, that's the Steam Input / "
              "8BitDo double-registration bug. Unplug and replug the adapter's "
              "dongle (or toggle Steam Input off/on for it), then run this "
              "again to confirm it collapses back to one slot.")
    if not args.watch:
        return
    print("watching for changes (Ctrl+C to stop)...")
    prev = slots
    try:
        while True:
            time.sleep(args.interval)
            slots, line = snapshot()
            if slots != prev:
                print(line)
                prev = slots
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
