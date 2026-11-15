"""Which inputs actually carry signal, and which are dead weight.

Reports, for the action head:
  * DEAD    - constant across the whole dataset, so it cannot inform anything
  * WEAK    - varies, but the model gives it ~no importance
  * USEFUL  - carries the decision

A feature can be dead for two very different reasons: the game never reports
it (a getter that always returns false), or the situation never arose in the
recordings. The first is a bug, the second is a data gap - the "coverage"
column separates them.
"""
import argparse
import collections
import glob

import numpy as np

from ajbot import dataset as D


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs/*.jsonl,botruns/*.jsonl")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--mode", default="transitions")
    args = ap.parse_args()

    ticks = []
    for pat in args.runs.split(","):
        ticks.extend(D.load(pat.strip()))
    if not ticks:
        print("no recordings found")
        return

    acts, chars = D.vocabularies(ticks)
    X, y, _, _ = D.build(ticks, acts, chars, mode=args.mode)
    names = D.feature_names(acts, chars)
    print(f"{len(ticks)} ticks -> {len(X)} samples, {X.shape[1]} features")
    if len(X) < 60:
        print("not enough samples")
        return

    # Dead = no variation at all.
    var = X.var(axis=0)
    nonzero = (X != 0).mean(axis=0)
    dead = [i for i in range(X.shape[1]) if var[i] == 0.0]

    from sklearn.ensemble import RandomForestClassifier
    rf = RandomForestClassifier(n_estimators=400, min_samples_leaf=2,
                                random_state=0).fit(X, y)
    imp = rf.feature_importances_

    order = np.argsort(-imp)
    print(f"\n=== most useful ({args.top}) ===")
    print(f"{'feature':<34}{'importance':>11}{'coverage':>10}")
    for i in order[:args.top]:
        print(f"  {names[i]:<32}{imp[i]:>11.4f}{nonzero[i]*100:>9.1f}%")

    live_weak = [i for i in order if var[i] > 0 and imp[i] < 0.001]
    print(f"\n=== weak but varying ({len(live_weak)}) ===")
    print("  " + ", ".join(names[i] for i in live_weak[:20]) or "  none")

    print(f"\n=== DEAD - constant, zero information ({len(dead)}) ===")
    by_kind = collections.Counter()
    for i in dead:
        by_kind["one-hot (character/action never seen)" if "=" in names[i]
                else "state field"] += 1
    for i in dead:
        if "=" not in names[i]:
            print(f"  {names[i]:<32} always {X[0, i]:g}")
    for k, c in by_kind.items():
        print(f"  ({c} {k})")

    total = imp.sum()
    top_share = imp[order[:10]].sum()
    print(f"\ntop 10 features carry {100*top_share/total:.0f}% of the decision")
    print(f"dead features: {len(dead)}/{X.shape[1]} "
          f"({100*len(dead)/X.shape[1]:.0f}%) - these can be pruned")


if __name__ == "__main__":
    main()
