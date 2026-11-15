"""Training-data scrubber: drop samples the recordings cannot label reliably.

Labels come from what the game reports (dataset.label_of). New recordings carry
new fields (`incl`, `ok_extreme`, ...) that make more decisions unambiguous;
older ones do not, and nothing is guessed to fill the gap. Instead, samples that
old data can only label wrongly or ambiguously are removed here, at training
time, so the recordings themselves stay exactly as they were recorded.

Each rule is `fn(me, nxt, label) -> reason or None`; a reason means "drop this
sample". Add a rule by decorating a function with @rule. `summary()` reports
what was dropped, so a scrub that eats too much is visible.
"""
import collections

RULES = []
STATS = collections.Counter()


def rule(fn):
    RULES.append(fn)
    return fn


@rule
def unlabelled_follow_up(me, nxt, label):
    """A follow-up recorded only as the move's own command again.

    Bakugo's TQ2 takes follow-ups on four different buttons, and the game reports
    each one as `cmd` still being ExUnique2 (ev_push rising). Without `incl` the
    button is unknown, and labelling it "ExUnique2" would teach a TQ2 re-cast.
    """
    from .dataset import FOLLOW_UP
    from .roster import base_name
    follow = FOLLOW_UP.get(base_name((me or {}).get("id")) or "")
    if not follow or (nxt or {}).get("cmd") != (me or {}).get("cmd"):
        return None
    if label in follow:                       # the bare command, repeated
        return "follow-up with no recorded choice"
    return None


@rule
def victory_pose(me, nxt, label):
    """The match is over; `cmd` is whatever was pressed last."""
    if (nxt or {}).get("act") == "WinDemo" or (me or {}).get("act") == "WinDemo":
        return "victory pose"
    return None


def keep(me, nxt, label):
    """True when the sample should be used for training."""
    for fn in RULES:
        why = fn(me, nxt, label)
        if why:
            STATS[why] += 1
            return False
    return True


def summary():
    if not STATS:
        return None
    return "scrubbed: " + ", ".join(f"{n} x {why}" for why, n in STATS.most_common())


def describe():
    """One line naming the active rules, for the trainer's log."""
    return "scrub rules: " + ", ".join(fn.__name__ for fn in RULES)


def main(argv=None):
    """python -m ajbot.scrub [glob ...] - how many samples each rule would drop."""
    import glob
    import sys
    from . import dataset as D
    pats = (argv if argv is not None else sys.argv[1:]) or ["runs/*.jsonl", "botruns/*.jsonl"]
    files = []
    for pat in pats:
        files += sorted(glob.glob(pat))
    total = 0
    for f in files:
        try:
            ticks = D.load(f)
        except (OSError, ValueError):
            continue
        D.stabilize(ticks)
        for i, t in enumerate(ticks[:-1]):
            me = D._rec(t, "me")
            key = D.key_of(t, "me")
            nxt = D._same(ticks[i + 1], key)
            if not me or not nxt:
                continue
            label = D.label_of(me, nxt)
            if label is None:
                continue
            total += 1
            keep(me, nxt, D.refine_dash(label, ticks, i, key))
    print(f"{len(files)} recordings, {total} labelled samples")
    print(summary() or "nothing scrubbed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
