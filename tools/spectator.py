"""Replay recordings through the CURRENT policy without touching the game,
and report where its guesses diverge from what the player actually did -
a spectator watching human play and predicting the next move, the way you'd
watch two chess players and guess their next one.

Restricted to HUMAN VS HUMAN matches by default: a recording being made by
a human (runs/*.jsonl) says nothing about who the OPPONENT was - training
mode against the CPU writes to the same folder. opponent_is_cpu() classifies
each file by how often the opponent reports ai=1 over the WHOLE match
(a single tick's ai flag is noisy - see ajbot.sampler - so this is a
majority vote, not a single reading), and anything that looks CPU-driven is
excluded and reported as skipped rather than silently folded in.

    python spectator.py                       # runs/*.jsonl, current policy
    python spectator.py --model models/prev/policy-20260101-000000.pkl
    python spectator.py --top-misses 30
    python spectator.py --include-cpu         # turn the filter off

This reuses build_weighted()'s exact tick-selection logic (dataset.py) so
"a decision" means the same thing here as it does in training - just with
per-sample context (character, distance, hp lead, situation) captured
alongside, which training's own arrays() throws away.
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import collections
import glob
import pickle
from pathlib import Path

import numpy as np

from ajbot import dataset as D
from ajbot.roster import base_name
from ajbot.sampler import act_class


def opponent_is_cpu(ticks, ai_threshold=0.5):
    """True if the recorded opponent looks CPU-controlled, aggregated over
    the whole match. A single tick's `ai` flag is noisy (see sampler.py's
    orientation notes), but a real CPU opponent should read ai=1 on the
    clear majority of ticks, and a real human essentially never."""
    D.stabilize(ticks)
    total = ai_votes = 0
    for t in ticks:
        en = D._rec(t, "enemy")
        if not en:
            continue
        total += 1
        if en.get("ai"):
            ai_votes += 1
    return total > 0 and (ai_votes / total) >= ai_threshold


def replay(path, agent_actions, agent_chars):
    """Yield one dict per decision in `path`: features, the label actually
    taken, and situational context - character, distance, hp lead, whether
    either fighter was mid-attack. Mirrors dataset.build_weighted()'s tick
    selection exactly, with per-sample metadata build_weighted discards."""
    ticks = D.load(path)
    if len(ticks) < 500:
        return
    D.stabilize(ticks)
    idx = {a: i for i, a in enumerate(agent_actions)}

    prev_cmd, last_cmd, last_key, key_start = None, None, None, 0
    for i, t in enumerate(ticks[:-1]):
        me = D._rec(t, "me")
        key = D.key_of(t, "me")
        if key != last_key:
            prev_cmd, last_cmd, last_key, key_start = None, None, key, i
        cur = me.get("cmd") if me else None
        if cur != last_cmd:
            prev_cmd, last_cmd = last_cmd, cur
        if not me:
            continue
        nxt = D._same(ticks[i + 1], key)
        if not nxt:
            continue
        label = D.label_of(me, nxt)
        if label is None or label in D.IDLE or label not in idx:
            continue

        history = ticks[max(key_start, i - D.HISTORY_K):i]
        x = D.featurize(t, agent_actions, agent_chars, prev_cmd, history)
        en = D._rec(t, "enemy")
        dist = me.get("dist") if isinstance(me.get("dist"), (int, float)) else None
        hp_lead = None
        if isinstance(me.get("hp"), (int, float)) and isinstance(en.get("hp"), (int, float)):
            hp_lead = me["hp"] - en["hp"]
        yield {
            "x": x, "label_idx": idx[label], "label": label,
            "character": base_name(key) or key,
            "key": key, "tick": i, "t": t.get("t"),
            "dist": dist, "hp_lead": hp_lead,
            "en_attacking": act_class(en.get("act")) in D.ATTACK_CLASSES if "act" in en else None,
            "me_hit": act_class(me.get("act")) == "damage" if "act" in me else None,
            "path": path,
        }


def bucket(sample):
    """One coarse situational tag per sample, for the accuracy breakdown."""
    tags = []
    if sample["en_attacking"]:
        tags.append("opponent mid-attack")
    if sample["me_hit"]:
        tags.append("we're mid-hitstun")
    if sample["dist"] is not None:
        if sample["dist"] < 150:
            tags.append("close range")
        elif sample["dist"] > 400:
            tags.append("far range")
        else:
            tags.append("mid range")
    if sample["hp_lead"] is not None:
        if sample["hp_lead"] > 0.2:
            tags.append("winning")
        elif sample["hp_lead"] < -0.2:
            tags.append("losing")
        else:
            tags.append("even health")
    return tags or ["unclassified"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs/*.jsonl",
                    help="glob - defaults to human recordings only, never botruns")
    ap.add_argument("--model", default="models/policy.pkl")
    ap.add_argument("--include-cpu", action="store_true",
                    help="skip the human-opponent filter (evaluate everything found)")
    ap.add_argument("--top-misses", type=int, default=15,
                    help="highest-confidence WRONG predictions to print")
    args = ap.parse_args()

    with open(args.model, "rb") as fh:
        policy = pickle.load(fh)
    action_model = policy["action"]
    actions, chars = policy["actions"], policy["chars"]
    print(f"policy: {len(actions)} actions, {len(chars)} characters, "
          f"{policy.get('n_features')} features")
    if not hasattr(action_model, "predict_proba"):
        print("this policy's action head has no predict_proba - cannot rank misses")
        return

    files = sorted(glob.glob(args.runs))
    if not files:
        print("no recordings found")
        return

    kept_files, skipped = [], []
    for f in files:
        ticks = D.load(f)
        if not args.include_cpu and opponent_is_cpu(ticks):
            skipped.append(f)
        else:
            kept_files.append(f)

    print(f"\n{len(kept_files)}/{len(files)} recordings kept as human-vs-human")
    if skipped:
        print(f"  excluded (opponent looked CPU-controlled): "
              f"{', '.join(Path(f).name for f in skipped)}")
    if not kept_files:
        print("\nnothing to evaluate - every recording looked like it was against the CPU")
        return

    samples = []
    for f in kept_files:
        samples.extend(replay(f, actions, chars))
    if not samples:
        print("\nno decisions found in the kept recordings")
        return
    print(f"{len(samples)} decisions across {len(kept_files)} match(es)\n")

    X = np.stack([s["x"] for s in samples])
    probs = action_model.predict_proba(X)
    preds = probs.argmax(axis=1)

    correct = 0
    misses = []
    per_char = collections.defaultdict(lambda: [0, 0])
    per_bucket = collections.defaultdict(lambda: [0, 0])
    per_actual = collections.defaultdict(lambda: [0, 0])
    for s, p, prob_row in zip(samples, preds, probs):
        actual_i = s["label_idx"]
        hit = int(p == actual_i)
        correct += hit
        per_char[s["character"]][0] += hit
        per_char[s["character"]][1] += 1
        per_actual[s["label"]][0] += hit
        per_actual[s["label"]][1] += 1
        for tag in bucket(s):
            per_bucket[tag][0] += hit
            per_bucket[tag][1] += 1
        if not hit:
            misses.append((prob_row[p], actions[p], s))

    acc = correct / len(samples)
    maj = collections.Counter(s["label"] for s in samples).most_common(1)[0]
    maj_acc = maj[1] / len(samples)
    print(f"overall: {acc*100:.1f}% match the human's actual choice  "
          f"(always-{maj[0]} baseline: {maj_acc*100:.1f}%)")

    print("\nby character:")
    for name, (hits, n) in sorted(per_char.items(), key=lambda kv: -kv[1][1]):
        print(f"  {name:<20} {hits}/{n}  {100*hits/n:.0f}%")

    print("\nby situation:")
    for tag, (hits, n) in sorted(per_bucket.items(), key=lambda kv: -kv[1][1]):
        print(f"  {tag:<22} {hits}/{n}  {100*hits/n:.0f}%")

    print("\nby actual action taken (what the model is worst at anticipating):")
    for name, (hits, n) in sorted(per_actual.items(), key=lambda kv: kv[1][0] / kv[1][1]):
        print(f"  {name:<16} {hits}/{n}  {100*hits/n:.0f}%")

    misses.sort(key=lambda m: -m[0])
    print(f"\nbiggest misses (confident but wrong), top {args.top_misses}:")
    for conf, predicted, s in misses[:args.top_misses]:
        print(f"  {Path(s['path']).name} tick {s['tick']:<6} {s['character']:<14} "
              f"guessed {predicted:<14} ({conf*100:.0f}% sure)  actually {s['label']:<14} "
              f"dist={s['dist']} hp_lead={s['hp_lead']}  {', '.join(bucket(s))}")


if __name__ == "__main__":
    main()
