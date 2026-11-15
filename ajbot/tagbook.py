"""The tag book: who to bring in on a Change Combo, and what they open with.

Every Change Combo (a partner tagged in while the victim is still in hitstun,
see ajbot.chain) is one sample of

    (outgoing fighter, incoming fighter, victim airborne?) -> hits and damage
                                                              the chain landed
                                                              after the tag

plus the incoming fighter's first attack (its opener) and how that went.

Two sources, added together:

  prior   the "handoff" / "opener" pages of models/combobook.json, mined from
          every recording (human play, both bots, the CPU's combos on us) by
          combobook.py on the server.
  live    models/tagbook.json, updated by play.py / play2.py at the end of
          each match from the bot's own Change Combos. No retrain needed: the
          next match already uses it.

TagPolicy asks `score()` for each partner that can be called and brings in the
best one; with few samples a pair's score shrinks toward the overall mean, so
the old rule (healthiest partner first) still decides until the data says a
pairing is better.
"""
import json
import os
import threading
from pathlib import Path

TAGBOOK_PATH = Path("models") / "tagbook.json"
# Pseudo-samples at the overall mean: a pair needs a handful of real samples
# before it can move the pick.
SHRINK = 4.0
# Openers need this many samples before one is suggested.
OPENER_MIN = 3

_lock = threading.Lock()


def _air_key(air):
    return "air" if air else "gnd" if air is not None else "?"


class TagBook:
    def __init__(self, combobook=None, path=TAGBOOK_PATH):
        self.path = Path(path)
        # "out|in|air" -> [n, hits_after_sum, dmg_after_sum]
        self.prior = {}
        self.prior_open = {}         # "in|air" -> {act: [n, hits_after_sum]}
        for k, v in ((combobook or {}).get("handoff") or {}).items():
            n = v[0]
            self.prior[k] = [n, v[1] * n, (v[2] if len(v) > 2 else 0.0) * n]
        for k, acts in ((combobook or {}).get("opener") or {}).items():
            self.prior_open[k] = {a: [e[0], e[1] * e[0]] for a, e in acts.items()}
        self.live = {"handoff": {}, "opener": {}}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.live["handoff"] = d.get("handoff") or {}
            self.live["opener"] = d.get("opener") or {}
        except (OSError, ValueError):
            pass
        self.dirty = False
        self.picks = {"book": 0, "health": 0}

    # ---- lookup ----

    def _pair(self, out, inc, air):
        n = h = dmg = 0.0
        keys = [f"{out}|{inc}|{_air_key(air)}"]
        if air is None:
            keys = [f"{out}|{inc}|air", f"{out}|{inc}|gnd", f"{out}|{inc}|?"]
        for k in keys:
            for src in (self.prior, self.live["handoff"]):
                e = src.get(k)
                if e:
                    n += e[0]
                    h += e[1]
                    dmg += e[2]
        return n, h, dmg

    def _mean(self):
        n = h = 0.0
        for src in (self.prior, self.live["handoff"]):
            for e in src.values():
                n += e[0]
                h += e[1]
        return h / n if n else 3.0

    def score(self, out, inc, air=None):
        """(expected hits after the tag, samples) for bringing `inc` in from
        `out`, shrunk toward the overall mean when samples are few."""
        n, h, _ = self._pair(out, inc, air)
        m = self._mean()
        return (h + SHRINK * m) / (n + SHRINK), n

    def opener(self, inc, air, available):
        """The incoming fighter's best first attack after a Change Combo, or
        None. `available(act)` filters to moves it can do right now."""
        merged = {}
        for src in (self.prior_open, self.live["opener"]):
            for k in ([f"{inc}|{_air_key(air)}"] if air is not None
                      else [f"{inc}|air", f"{inc}|gnd", f"{inc}|?"]):
                for a, e in (src.get(k) or {}).items():
                    m = merged.setdefault(a, [0, 0.0])
                    m[0] += e[0]
                    m[1] += e[1]
        good = [(e[1] / e[0], a) for a, e in merged.items()
                if e[0] >= OPENER_MIN and available(a)]
        return max(good)[1] if good else None

    # ---- learning ----

    def observe(self, combos):
        """Fold one match's finished combos (ours, ajbot.chain.Combo) in."""
        for c in combos:
            for p in getattr(c, "tag_points", ()):
                inc = p.get("in")
                if not inc or not p.get("char") or p["char"] == "?":
                    continue
                after = c.hits - p.get("hits_at", 0)
                dmg = max(0.0, c.damage - p.get("dmg_at", c.damage))
                k = f"{p['char']}|{inc}|{_air_key(p.get('air'))}"
                e = self.live["handoff"].setdefault(k, [0, 0.0, 0.0])
                e[0] += 1
                e[1] += after
                e[2] = round(e[2] + dmg, 4)
                if p.get("opener"):
                    ko = f"{inc}|{_air_key(p.get('air'))}"
                    o = self.live["opener"].setdefault(ko, {}).setdefault(p["opener"], [0, 0.0])
                    o[0] += 1
                    o[1] += after
                self.dirty = True

    def save(self):
        if not self.dirty:
            return
        with _lock:
            # Two bots in one process share this object; another process may
            # have written since we loaded - merge counts written by others
            # is not attempted, last writer wins (each process owns its run).
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.live, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        self.dirty = False

    def summary(self):
        n = sum(e[0] for e in self.live["handoff"].values())
        return (f"tag book: {len(self.prior)} mined pairs, {n} live handoffs; "
                f"partner picked by book {self.picks['book']}, by health {self.picks['health']}")


_shared = None


def get(combobook=None):
    """One TagBook per process (both bots in play2 learn into the same one)."""
    global _shared
    if _shared is None:
        _shared = TagBook(combobook)
    return _shared
