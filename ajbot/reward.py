"""Scoring a recording against the bot's goals.

Two goals, from how All's Justice actually rewards play:

  tagging  - switching between characters mid-combo is what builds Plus Ultra
  damage   - the combo routes that take the most health per opening

Both are measured PER CHARACTER rather than through the `me`/`enemy` pointers.
Those pointers pick whichever of a side ran a script most recently, so they
flip several times a second - reading damage through them invented 1010 "tag
switches" and a single 0.68 hp hit in one recording.

Also note `active` is not "the character you are controlling": up to eleven
characters report active=1 at once. The controlled fighter is the one whose
lever is live (`lon`/`lscale`), because only it reads a stick.

There is no gauge getter in the Lua API, so Plus Ultra is observed through
threshold flags (`pu_ready`, `awk_full`) rather than a meter value.
"""
import json
from collections import Counter, defaultdict

# A single tick must not drop more health than this to count as damage;
# anything larger is a round reset, not a hit.
MAX_HIT = 0.5
# Ticks without damage before a combo is considered finished.
COMBO_GAP = 12


def load(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def controlled_at(tick):
    """Key of the character being driven right now, or None.

    Identified by a live lever: only the controlled fighter reads a stick.
    `active` cannot be used - up to eleven characters report active=1 at once.
    """
    best, bestv = None, -1.0
    for k, c in tick.get("chars", {}).items():
        if c.get("enemy"):
            continue
        v = float(c.get("lscale") or 0.0)
        if c.get("lon") and v > bestv:
            best, bestv = k, v
    return best


def controlled_series(ticks):
    """Controlled character per tick, carried forward through neutral stick.

    The lever only reads while it is actually pushed, and during a combo the
    stick is often neutral while buttons are mashed - so a per-tick lookup is
    blank most of the time. Holding the last known fighter until another one
    takes the stick is what makes tag detection possible at all.
    """
    out, cur = [], None
    for t in ticks:
        k = controlled_at(t)
        if k:
            cur = k
        out.append(cur)
    return out


def damage_events(ticks):
    """[(tick_index, victim, amount)] from per-character health drops.

    Also returns the ticks where a character's health went UP. Training mode
    refills health rather than allowing a KO, and without treating a refill as
    a hard break a single "combo" ran straight through it and reported 3.81
    damage on a 0..1 scale.
    """
    prev, out, resets = {}, [], set()
    for i, t in enumerate(ticks):
        for k, c in t.get("chars", {}).items():
            h = c.get("hp")
            if not isinstance(h, (int, float)):
                continue
            if k in prev:
                d = prev[k] - h
                if 0.0005 < d < MAX_HIT:
                    out.append((i, k, d))
                elif d < -0.01:
                    resets.add((i, k))
            prev[k] = h
    return out, resets


def combos(ticks, side="enemy", ctrl=None):
    """Group damage on one side into combos.

    Returns [{start, end, damage, hits, tags, characters}] where `tags` counts
    how many times the controlled character changed mid-combo - which is the
    behaviour the Plus Ultra economy rewards.
    """
    enemies = {k for t in ticks for k, c in t.get("chars", {}).items()
               if (c.get("enemy") if side == "enemy" else not c.get("enemy"))}
    all_events, resets = damage_events(ticks)
    events = [e for e in all_events if e[1] in enemies]
    if not events:
        return []
    reset_at = {}
    for i, k in resets:
        reset_at.setdefault(k, []).append(i)

    # Group per victim. Pooling every enemy into one timeline let damage on
    # three different characters merge into a single "combo", which produced a
    # 3.81 best_combo on a 0..1 health scale.
    out = []
    open_combo = {}
    for i, victim, d in events:
        cur = open_combo.get(victim)
        healed = any(cur["end"] < r <= i for r in reset_at.get(victim, [])) if cur else False
        if cur and not healed and i - cur["end"] <= COMBO_GAP:
            cur["end"] = i
            cur["damage"] += d
            cur["hits"] += 1
        else:
            if cur:
                out.append(cur)
            open_combo[victim] = {"start": i, "end": i, "damage": d,
                                  "hits": 1, "victim": victim}
    out.extend(c for c in open_combo.values() if c)
    out.sort(key=lambda c: c["start"])

    ctrl = ctrl or controlled_series(ticks)
    for c in out:
        seen = []
        for i in range(c["start"], min(c["end"] + 1, len(ticks))):
            k = ctrl[i]
            if k and (not seen or seen[-1] != k):
                seen.append(k)
        c["characters"] = seen
        c["tags"] = max(0, len(seen) - 1)
    return out


def pu_events(ticks):
    """Ticks where any of my characters became Plus-Ultra ready."""
    was, out = {}, []
    for i, t in enumerate(ticks):
        for k, c in t.get("chars", {}).items():
            if c.get("enemy"):
                continue
            now = bool(c.get("pu_ready"))
            if now and not was.get(k, False):
                out.append((i, k))
            was[k] = now
    return out


def score(ticks):
    """Headline numbers for one recording."""
    ctrl = controlled_series(ticks)
    cs = combos(ticks, ctrl=ctrl)
    dmg = sum(c["damage"] for c in cs)
    tagged = [c for c in cs if c["tags"] > 0]
    return {
        "ticks": len(ticks),
        "combos": len(cs),
        "damage": round(dmg, 3),
        "best_combo": round(max((c["damage"] for c in cs), default=0.0), 3),
        "mean_combo": round(dmg / len(cs), 4) if cs else 0.0,
        "hits_per_combo": round(sum(c["hits"] for c in cs) / len(cs), 2) if cs else 0.0,
        "combos_with_tag": len(tagged),
        "tag_rate": round(len(tagged) / len(cs), 3) if cs else 0.0,
        "damage_tagged": round(sum(c["damage"] for c in tagged) / len(tagged), 4) if tagged else 0.0,
        "damage_untagged": round(
            sum(c["damage"] for c in cs if not c["tags"])
            / max(1, len(cs) - len(tagged)), 4),
        "pu_ready_events": len(pu_events(ticks)),
        "tag_switches": sum(1 for a, b in zip(ctrl, ctrl[1:]) if a and b and a != b),
    }


def best_routes(ticks, top=10):
    """Highest-damage combos, with the action sequence that produced them."""
    ctrl = controlled_series(ticks)
    out = []
    for c in sorted(combos(ticks, ctrl=ctrl), key=lambda c: -c["damage"])[:top]:
        acts = []
        for i in range(c["start"], min(c["end"] + 1, len(ticks))):
            k = ctrl[i]
            a = (ticks[i].get("chars", {}).get(k) or {}).get("cmd")
            if a and a != "None" and (not acts or acts[-1] != a):
                acts.append(a)
        out.append({"damage": round(c["damage"], 3), "hits": c["hits"],
                    "tags": c["tags"], "actions": acts,
                    "characters": c.get("characters", [])})
    return out
