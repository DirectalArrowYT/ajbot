"""First visible bot: drives a character through scripted Aizawa routines.

Usage:
    python demo_bot.py            # run all routines
    python demo_bot.py jab        # run one routine
    python demo_bot.py --list

Start a local/training match, then alt-tab back and run this.
The virtual pad appears as an extra Xbox controller, so assign it to the
slot you want the bot to drive (P1 or P2).
"""
import sys
import time

from ajbot import Pad, NEUTRAL, LEFT, RIGHT

COUNTDOWN = 5


def jab(p):
    """Basic A string. Aizawa's A1/A2 are ~13f startup; A2 cancels from ~f23."""
    for _ in range(3):
        p.do("A", hold_frames=3, recover_frames=14)


def capture(p):
    """A > A > Q1 — jab confirm into the scarf bind."""
    p.do("A", hold_frames=3, recover_frames=16)
    p.do("A", hold_frames=3, recover_frames=14)
    p.do("Q1", hold_frames=4, recover_frames=50)


def dash_in(p):
    """Close distance, then dash attack."""
    p.stick(RIGHT)
    p.do("DASH", hold_frames=3, recover_frames=0)
    p.tick(18)
    p.stick(NEUTRAL)
    p.do("A", hold_frames=3, recover_frames=20)


def tilt(p):
    """Tilt-quirk: stick direction held with the quirk button."""
    p.do("TQ", hold_frames=4, recover_frames=45, direction=RIGHT)


def unblockable(p):
    """UB is ~35f startup on Aizawa — slow, telegraphed."""
    p.do("UB", hold_frames=4, recover_frames=60)


def turtle(p):
    """Hold guard, then step back."""
    p.stick(NEUTRAL)
    p.do("GUARD", hold_frames=40)
    p.move(LEFT, 20)


def footsies(p):
    """Shuffle in and out of range, poking."""
    for _ in range(3):
        p.move(RIGHT, 14)
        p.do("A", hold_frames=3, recover_frames=16)
        p.move(LEFT, 18)


ROUTINES = {
    "jab": jab,
    "capture": capture,
    "dash_in": dash_in,
    "tilt": tilt,
    "ub": unblockable,
    "turtle": turtle,
    "footsies": footsies,
}


def main():
    args = [a for a in sys.argv[1:]]
    if "--list" in args:
        print("routines:", ", ".join(ROUTINES))
        return

    names = [a for a in args if not a.startswith("-")] or list(ROUTINES)
    unknown = [n for n in names if n not in ROUTINES]
    if unknown:
        print(f"unknown routine(s): {', '.join(unknown)}")
        print("available:", ", ".join(ROUTINES))
        return

    pad = Pad()
    print("virtual pad created - make sure it's assigned to the slot you want driven")
    for i in range(COUNTDOWN, 0, -1):
        print(f"  starting in {i}...", end="\r", flush=True)
        time.sleep(1)
    print(" " * 40, end="\r")

    try:
        for name in names:
            print(f"[{pad.frame:6d}] {name}")
            pad.idle(20)
            ROUTINES[name](pad)
        pad.idle(30)
    finally:
        pad.reset()
        print("done - pad released")


if __name__ == "__main__":
    main()
