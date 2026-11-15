"""Mine recordings for actions ACTION_MAP has no button press for, and guess
one when the name says it is a held variant of a move already mapped.

    python learn_actions.py                  # scan every recording, write
                                              # models/learned_actions.json

Charge quirks are the case this actually solves: the game reports a distinct
`cmd` ('UniqueCharge1') for holding the same button that fires 'Unique1'
tapped, and a recording shows how long it stayed that way before something
happened. That duration becomes the hold_frames the pad uses to attempt it.

Anything that does not match a known base move (no "Charge" suffix, or the
tapped version is not in ACTION_MAP either) is written to
logs/unknown_actions.jsonl instead of guessed at - a wrong button is worse
than no button, and those need a human to look at the move and add it to
ACTION_MAP by hand.

Re-running this only adds or refreshes entries for actions seen in the runs
scanned; it never touches hand-written ACTION_MAP entries (agent.py's loader
only fills gaps that ACTION_MAP does not already cover).
"""
import argparse
import glob
import json
import re
from pathlib import Path

from ajbot import dataset as D
from ajbot.agent import ACTION_MAP, BUTTON_BY_NAME
from ajbot.action_log import UnknownActionLog

OUT_PATH = Path("models") / "learned_actions.json"

_NAME_BY_BUTTON = {v: k for k, v in BUTTON_BY_NAME.items()}

CHARGE_RE = re.compile(r"^(.*?)Charge(\d*)$")


def _base_action(name):
    """'UniqueCharge1' -> 'Unique1': the tapped move the same button fires,
    if the naming says this is its held variant and that move is mapped."""
    m = CHARGE_RE.match(name)
    if not m:
        return None
    base = f"{m.group(1)}{m.group(2)}"
    return base if base in ACTION_MAP else None


def _runs(ticks):
    """{cmd: [seconds held, ...]} - how long `me.cmd` stayed on each value it
    took, from tick timestamps rather than tick counts, since a recording's
    sample rate varies."""
    out = {}
    cur, start = None, None
    for t in ticks:
        me = D._rec(t, "me")
        cmd = me.get("cmd") if me else None
        ts = t.get("t")
        if cmd != cur:
            if cur and start is not None and ts is not None:
                out.setdefault(cur, []).append(ts - start)
            cur, start = cmd, ts
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=None, help="glob; default = every recording")
    args = ap.parse_args()

    files = (sorted(glob.glob(args.runs)) if args.runs else
             sorted(glob.glob("runs/*.jsonl")) + sorted(glob.glob("botruns/*.jsonl")))
    if not files:
        print("no recordings found")
        return

    durations = {}     # action -> list of hold durations, seconds
    for f in files:
        ticks = D.load(f)
        for cmd, durs in _runs(ticks).items():
            if cmd and cmd not in ACTION_MAP:
                durations.setdefault(cmd, []).extend(durs)

    if not durations:
        print(f"{len(files)} recordings scanned; nothing unmapped found - "
              "ACTION_MAP already covers every action seen")
        return

    learned = {}
    unresolved = UnknownActionLog(source="learn_actions", quiet=True)
    for action, durs in sorted(durations.items()):
        n = len(durs)
        avg_s = sum(durs) / n
        base = _base_action(action)
        if base is None:
            unresolved.observe(action, reason=f"no known base move "
                               f"({n} samples, avg hold {avg_s:.2f}s)")
            print(f"  {action:<20} {n:4d} samples, avg {avg_s:.2f}s  "
                  f"-> unresolved (logged to {unresolved.path})")
            continue
        buttons, needs_stick = ACTION_MAP[base]
        hold_frames = max(6, min(90, round(avg_s * 60)))
        learned[action] = {
            "buttons": [_NAME_BY_BUTTON[b] for b in buttons],
            "needs_stick": needs_stick,
            "hold_frames": hold_frames,
            "samples": n,
            "avg_hold_s": round(avg_s, 3),
            "learned_from": base,
        }
        print(f"  {action:<20} {n:4d} samples, avg {avg_s:.2f}s -> same "
              f"buttons as {base}, held {hold_frames} frames")
    unresolved.close()

    if not learned:
        print(f"\nnothing could be resolved automatically - see {unresolved.path}")
        return

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    existing.update(learned)
    OUT_PATH.write_text(json.dumps(existing, indent=2, sort_keys=True),
                        encoding="utf-8")
    print(f"\nwrote {len(learned)} learned mapping(s) to {OUT_PATH}")


if __name__ == "__main__":
    main()
