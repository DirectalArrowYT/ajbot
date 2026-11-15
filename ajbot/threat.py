"""What the other side's moves do to us, and when our own resources come out -
mined from the recordings for the rule layers (defence, emergency escape,
Plus Ultra / Rising). Nothing here is a policy feature, so building or
rebuilding this table never touches the feature caches.

  contact  per (character, act, distance bin): of the times the act was
           started, how often the other team was touched (hit, guarded or
           just-guarded) within CONTACT_WINDOW_S, and how long after the start
           the first touch came. The defence used to answer only attacks
           started inside a fixed 450 units, so every dash-in string and every
           projectile from range went unanswered (the friend matches on
           2026-11-09: 58 of 140 target combos started at 450-900 landed, and
           the defence never looked). The rate says whether an attack started
           at this distance is a threat at all; the timing places the
           just-guard tap (the window is 0.192 s after the press, DB
           ActionParam ジャストガード.有効時間, and a second tap inside 1.0 s does
           nothing - 再入力無効時間 - so a guessed tap wastes the next one too).
  combo    per (attacker character, hits so far, attacker holding Plus Ultra):
           how much more damage a combo that has reached that many hits goes on
           to do. The emergency escape is worth spending where this is large.
  escape   per victim act: of the bot's escape presses in that act, how many
           came out (the friend matches: 14 of 29 - frozen and launched
           states refuse it). Plus, from every fighter, the act each real
           escape started from.
  res      per resource ('pu', 'rising') x the presser's act: of the bot's
           presses, how many came out within RES_WINDOW_S (the friend matches:
           4 of 20 Plus Ultras and 7 of 28 Risings - pressed mid-attack, they
           are dropped); plus, from every fighter, the act each real one
           started from ('from').

    python -m ajbot.threat                 # writes models/threat.json
    python -m ajbot.threat --show Bakugo
"""
import argparse
import glob
import json
import random
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import dataset as D, tickcache
from .roster import base_name
from .sampler import act_class

PATH = Path("models") / "threat.json"
VERSION = 2

DIST_EDGES = [0.0, 150.0, 300.0, 450.0, 650.0, 900.0, 1300.0, 1900.0, 2800.0]
CONTACT_WINDOW_S = 1.5
COMBO_GAP_S = 1.2          # same rule as the match reports' combo counter
ESC_WINDOW_S = 0.6
RES_WINDOW_S = 0.7
AFTER_ESC_S = 2.0
MAX_DT = 40                # contact timings kept per key per recording
HIT_BINS = 12              # hits-so-far is capped here
SHRINK = 5.0
OFFENCE = ("attack", "combo", "unblock", "pu", "assist", "rising")
TOUCH = ("ev_hit", "ev_gok", "ev_jg", "ev_js")


def bin_of(dist):
    for i in range(len(DIST_EDGES) - 1, -1, -1):
        if dist >= DIST_EDGES[i]:
            return i
    return 0


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def _delta(cur, prev, field):
    a, b = _num((cur or {}).get(field)), _num((prev or {}).get(field))
    if a is None or b is None:
        return 0.0
    return max(0.0, a - b)


def _started(cur, prev):
    """Did `cur` begin a new act since `prev` (same character, one tick apart)?"""
    if not cur or not prev:
        return False
    if cur.get("act") != prev.get("act"):
        return True
    a, b = _num(cur.get("act_n")), _num(prev.get("act_n"))
    return a is not None and b is not None and a < b


# ------------------------------------------------------------------ mining --

def _one(ticks):
    nb = len(DIST_EDGES)
    contact = defaultdict(lambda: {"n": 0, "c": 0, "bins": [[0, 0] for _ in range(nb)],
                                   "dt": [[] for _ in range(nb)]})
    combo = defaultdict(lambda: [0, 0.0, 0.0])           # n, sum, sum of squares
    esc_after = defaultdict(lambda: [0, 0.0])            # hits-bin -> n, dmg after
    esc_ok = defaultdict(lambda: [0, 0])                 # bot presses: n, came out
    esc_from = defaultdict(int)                          # every real escape
    res_ok = defaultdict(lambda: [0, 0])                 # (res, act) -> n, came out
    res_from = defaultdict(int)                          # (res, act) -> real starts
    if len(ticks) < 200:
        return contact, combo, esc_after, esc_ok, esc_from, res_ok, res_from

    D.stabilize(ticks)
    ts = [t.get("t", i / 15.0) for i, t in enumerate(ticks)]
    sides = []
    for t in ticks:
        me, en, mine, theirs = D.resolve(t)
        ms, th = set(mine or []), set(theirs or [])
        if me:
            ms.add(me)
        if en:
            th.add(en)
        sides.append((me, en, ms, th))

    def rec(i, k):
        r = (ticks[i].get("chars") or {}).get(k) if k else None
        if not r or r.get("age", 0) > D.MAX_AGE:
            return None
        return r

    def touched(i, team):
        """Did any of `team` get touched between tick i-1 and i?"""
        for k in team:
            a, b = rec(i, k), rec(i - 1, k)
            if a and b and any(_delta(a, b, f) > 0 for f in TOUCH):
                return True
        return False

    def team_hp(i, team):
        s = 0.0
        for k in team:
            r = rec(i, k)
            h = _num((r or {}).get("hp"))
            if h is not None:
                s += h
        return s

    n = len(ticks)
    # ---- contact: every attack start by anyone, with the other team as target
    for i in range(1, n):
        me, en, ms, th = sides[i]
        for team, other in ((ms, th), (th, ms)):
            for k in team:
                cur, prev = rec(i, k), rec(i - 1, k)
                if not _started(cur, prev) or act_class(cur.get("act")) not in OFFENCE:
                    continue
                d = _num(cur.get("dist"))
                if d is None or not other:
                    continue
                act = cur.get("act")
                hit_dt = None
                for j in range(i + 1, n):
                    if ts[j] - ts[i] > CONTACT_WINDOW_S:
                        break
                    if touched(j, other):
                        hit_dt = ts[j] - ts[i]
                        break
                    r = rec(j, k)
                    if r is None or r.get("act") != act:
                        # The attacker moved on (or was interrupted) before
                        # anything landed: this act did not connect.
                        break
                from .roster import move_char
                ch = move_char(cur, k)
                node = contact[(ch, act)]
                b = bin_of(d)
                node["n"] += 1
                node["bins"][b][0] += 1
                if hit_dt is not None:
                    node["c"] += 1
                    node["bins"][b][1] += 1
                    if len(node["dt"][b]) < MAX_DT:
                        node["dt"][b].append(round(hit_dt, 3))

    # ---- combos received, per victim team
    for vi in (0, 1):
        ep = None
        for i in range(1, n):
            me, en, ms, th = sides[i]
            victims = ms if vi == 0 else th
            att_key = en if vi == 0 else me
            hit = False
            for k in victims:
                a, b = rec(i, k), rec(i - 1, k)
                if a and b and _delta(a, b, "ev_hit") > 0 and act_class(a.get("act")) == "damage":
                    hit = True
            esc = any(_delta(rec(i, k), rec(i - 1, k), "ev_esc") > 0 for k in victims
                      if rec(i, k) and rec(i - 1, k))
            if ep is not None and (ts[i] - ep["last"] > COMBO_GAP_S):
                _close(ep, team_hp(i, ep["team"]), combo, esc_after)
                ep = None
            if hit:
                if ep is None:
                    ep = {"team": set(victims), "hp0": team_hp(i - 1, victims),
                          "hits": [], "last": ts[i], "esc": None}
                ep["last"] = ts[i]
                att = rec(i, att_key) or {}
                ep["hits"].append((base_name(att_key) or att_key or "?",
                                   1 if att.get("ok_pu1") else 0,
                                   ep["hp0"] - team_hp(i, ep["team"])))
            if esc and ep is not None and ep["esc"] is None:
                ep["esc"] = (len(ep["hits"]), ep["hp0"] - team_hp(i, ep["team"]), ts[i])
            if ep is not None and ep["esc"] is not None and "esc_end" not in ep \
                    and ts[i] - ep["esc"][2] >= AFTER_ESC_S:
                ep["esc_end"] = ep["hp0"] - team_hp(i, ep["team"])

    # ---- escapes and resources: what came out, from which act
    for i in range(1, n):
        t = ticks[i]
        me, en, ms, th = sides[i]
        for k in ms | th:
            cur, prev = rec(i, k), rec(i - 1, k)
            if not cur or not prev:
                continue
            if _delta(cur, prev, "ev_esc") > 0:
                esc_from[prev.get("act") or "None"] += 1
            if _delta(cur, prev, "ev_pu") > 0:
                res_from[("pu", prev.get("act") or "None")] += 1
            if cur.get("act") == "AttackAwaking" and prev.get("act") != "AttackAwaking"                     and act_class(prev.get("act")) != "damage"                     and not str(prev.get("act") or "").startswith("Sidekick"):
                # Out of hitstun or a partner switch it is the AUTOMATIC
                # Ultimate Rising (last fighter standing), not a press.
                res_from[("rising", prev.get("act") or "None")] += 1
        if not t.get("bot") or not me:
            continue
        mrec = rec(i, me)
        if not mrec:
            continue
        pressed = []
        if t.get("escape"):
            pressed.append("escape")
        if t.get("resource") == "pu" or (
                t.get("resource") == "rising" and mrec.get("act") != "AttackAwaking"
                and not mrec.get("awake")):
            # A "rising" logged while already rising was a wasted press on
            # top of the automatic Ultimate Rising, not a test of the button.
            pressed.append(t["resource"])
        for what in pressed:
            ok = False
            win = ESC_WINDOW_S if what == "escape" else RES_WINDOW_S
            for j in range(i + 1, n):
                if ts[j] - ts[i] > win:
                    break
                r2, r1 = rec(j, me), rec(j - 1, me)
                if not r2 or not r1:
                    continue
                if what == "escape" and _delta(r2, r1, "ev_esc") > 0:
                    ok = True
                elif what == "pu" and _delta(r2, r1, "ev_pu") > 0:
                    ok = True
                elif what == "rising" and r2.get("act") == "AttackAwaking" \
                        and r1.get("act") != "AttackAwaking":
                    ok = True
                if ok:
                    break
            act = mrec.get("act") or "None"
            if what == "escape":
                e = esc_ok[act]
            else:
                e = res_ok[(what, act)]
            e[0] += 1
            e[1] += int(ok)
    return contact, combo, esc_after, esc_ok, esc_from, res_ok, res_from


def _close(ep, hp_now, combo, esc_after):
    final = ep["hp0"] - hp_now
    for h, (ch, pu, so_far) in enumerate(ep["hits"], 1):
        if ep["esc"] is not None and h > ep["esc"][0]:
            break                      # after an escape the rest is not the same combo
        rem = max(0.0, final - so_far)
        if ep["esc"] is not None:
            continue                   # escaped: the remainder is not what it would have been
        hb = min(h, HIT_BINS)
        for key in ((ch, hb, pu), ("*", hb, pu)):
            c = combo[key]
            c[0] += 1
            c[1] += rem
            c[2] += rem * rem
    if ep["esc"] is not None:
        hb = min(max(ep["esc"][0], 1), HIT_BINS)
        after = ep.get("esc_end", final) - ep["esc"][1]
        e = esc_after[hb]
        e[0] += 1
        e[1] += max(0.0, after)


def _partial(path):
    def compute(p):
        try:
            ticks = tickcache.raw_ticks(p)
        except Exception:
            return None
        contact, combo, esc_after, esc_ok, esc_from, res_ok, res_from = _one(ticks)
        return {
            "contact": [[c, a, v["n"], v["c"], v["bins"], v["dt"]] for (c, a), v in contact.items()],
            "combo": [[c, h, pu, v[0], v[1], v[2]] for (c, h, pu), v in combo.items()],
            "esc_after": [[h, v[0], v[1]] for h, v in esc_after.items()],
            "esc_ok": [[a, v[0], v[1]] for a, v in esc_ok.items()],
            "esc_from": [[a, v] for a, v in esc_from.items()],
            "res_ok": [[r, a, v[0], v[1]] for (r, a), v in res_ok.items()],
            "res_from": [[r, a, v] for (r, a), v in res_from.items()],
        }
    return tickcache.cached_fact(path, "threat", compute, version=VERSION)


def mine(files, max_files=None, seed=0, verbose=True):
    files = list(files)
    random.Random(seed).shuffle(files)
    if max_files:
        files = files[:max_files]
    nb = len(DIST_EDGES)
    contact = {}
    combo = defaultdict(lambda: [0, 0.0, 0.0])
    esc_after = defaultdict(lambda: [0, 0.0])
    esc_ok = defaultdict(lambda: [0, 0])
    esc_from = defaultdict(int)
    res_ok = defaultdict(lambda: [0, 0])
    res_from = defaultdict(int)
    from concurrent.futures import ProcessPoolExecutor
    import os
    from .parallel import safe_worker_count
    sizes = tickcache.src_sizes(files)
    used = 0
    with ProcessPoolExecutor(max_workers=safe_worker_count(sizes, os.cpu_count() or 4)) as ex:
        for fi, part in enumerate(ex.map(_partial, files), 1):
            if verbose and fi % 100 == 0:
                print(f"  threat: {fi}/{len(files)} recordings", flush=True)
            if not part:
                continue
            used += 1
            for c, a, nn, cc, bins, dts in part["contact"]:
                node = contact.setdefault(c, {}).setdefault(a, {
                    "n": 0, "c": 0, "bins": [[0, 0] for _ in range(nb)],
                    "dt": [[] for _ in range(nb)]})
                node["n"] += nn
                node["c"] += cc
                for b in range(nb):
                    node["bins"][b][0] += bins[b][0]
                    node["bins"][b][1] += bins[b][1]
                    node["dt"][b].extend(dts[b])
            for c, h, pu, nn, s, s2 in part["combo"]:
                v = combo[(c, h, pu)]
                v[0] += nn
                v[1] += s
                v[2] += s2
            for h, nn, s in part["esc_after"]:
                v = esc_after[h]
                v[0] += nn
                v[1] += s
            for a, nn, ok in part["esc_ok"]:
                esc_ok[a][0] += nn
                esc_ok[a][1] += ok
            for a, nn in part["esc_from"]:
                esc_from[a] += nn
            for r, a, nn, ok in part["res_ok"]:
                res_ok[(r, a)][0] += nn
                res_ok[(r, a)][1] += ok
            for r, a, nn in part["res_from"]:
                res_from[(r, a)] += nn

    # Timing: quartiles per bin, the raw lists would be megabytes.
    for c, acts in contact.items():
        for a, node in acts.items():
            q = []
            for b in range(nb):
                v = node["dt"][b]
                q.append([len(v)] + ([round(float(x), 3) for x in np.percentile(v, [25, 50, 75])]
                                     if v else [None, None, None]))
            node["dt"] = q
    out = {
        "version": VERSION,
        "files": used,
        "dist_edges": DIST_EDGES,
        "contact": contact,
        "combo": {f"{c}|{h}|{pu}": [v[0], v[1], v[2]] for (c, h, pu), v in combo.items()},
        "esc_after": {str(h): v for h, v in esc_after.items()},
        "esc_ok": {a: v for a, v in esc_ok.items()},
        "esc_from": dict(esc_from),
        "res_ok": {f"{r}|{a}": v for (r, a), v in res_ok.items()},
        "res_from": {f"{r}|{a}": v for (r, a), v in res_from.items()},
    }
    return out


# ------------------------------------------------------------------ lookup --

class Threat:
    def __init__(self, data=None):
        data = data or {}
        if data and data.get("dist_edges") != DIST_EDGES:
            print("  [threat] table built with other distance bins - ignored until "
                  "rebuilt (python -m ajbot.threat)", flush=True)
            data = {}
        self.data = data
        self.contact = data.get("contact") or {}
        # Per act name, every character pooled: the parent rate when a
        # character has seen the act only a few times.
        self._act = {}
        nb = len(DIST_EDGES)
        for acts in self.contact.values():
            for a, node in acts.items():
                g = self._act.setdefault(a, {"n": 0, "c": 0, "bins": [[0, 0] for _ in range(nb)]})
                g["n"] += node["n"]
                g["c"] += node["c"]
                for b in range(nb):
                    g["bins"][b][0] += node["bins"][b][0]
                    g["bins"][b][1] += node["bins"][b][1]
        tot_n = sum(g["n"] for g in self._act.values())
        tot_c = sum(g["c"] for g in self._act.values())
        self.base = (tot_c + 1.0) / (tot_n + 3.0) if tot_n else 0.3

    def __bool__(self):
        return bool(self.contact)

    @staticmethod
    def _shrink(hit, n, parent):
        return (hit + SHRINK * parent) / (n + SHRINK)

    def p_contact(self, char, act, dist):
        """Chance an `act` started by `char` at `dist` touches us within
        CONTACT_WINDOW_S, or None when the act was never seen."""
        if not isinstance(dist, (int, float)):
            return None
        g = self._act.get(act)
        node = (self.contact.get(char) or {}).get(act)
        if not g and not node:
            return None
        b = bin_of(dist)
        p_g = self._shrink(g["c"], g["n"], self.base) if g else self.base
        if g:
            p_g = self._shrink(g["bins"][b][1], g["bins"][b][0], p_g)
        if not node:
            return p_g
        p_c = self._shrink(node["c"], node["n"], p_g)
        return self._shrink(node["bins"][b][1], node["bins"][b][0], p_c)

    def contact_time(self, char, act, dist):
        """(p25, p50, p75) seconds from the act's start to the first touch, from
        the nearest distance bin with data, or None."""
        node = (self.contact.get(char) or {}).get(act)
        if not node or not isinstance(dist, (int, float)):
            return None
        q = node.get("dt") or []
        b = bin_of(dist)
        for off in (0, -1, 1, -2, 2):
            j = b + off
            if 0 <= j < len(q) and q[j] and q[j][0] >= 3 and q[j][2] is not None:
                return tuple(q[j][1:4])
        # Every distance pooled, weighted by samples.
        tot = [(x[0], x[2]) for x in q if x and x[0] and x[2] is not None]
        if not tot:
            return None
        w = sum(n for n, _ in tot)
        if w < 3:
            return None
        med = sum(n * m for n, m in tot) / w
        return (med * 0.8, med, med * 1.25)

    def combo_remaining(self, char, hits, pu=0):
        """Expected further team-health loss of a combo `char` has landed
        `hits` hits of. None with no data."""
        c = self.data.get("combo") or {}
        hb = min(max(int(hits), 1), HIT_BINS)
        g = c.get(f"*|{hb}|{int(bool(pu))}") or c.get(f"*|{hb}|0")
        if not g or g[0] < 5:
            return None
        pg = g[1] / g[0]
        node = c.get(f"{char}|{hb}|{int(bool(pu))}")
        if not node:
            return pg
        return (node[1] + SHRINK * pg) / (node[0] + SHRINK)

    def escape_ok(self, act):
        """Chance an escape pressed during `act` comes out (bot presses, with
        every fighter's real escapes as a weak positive), or None."""
        e = (self.data.get("esc_ok") or {}).get(act)
        seen = (self.data.get("esc_from") or {}).get(act, 0)
        if not e and not seen:
            return None
        n, ok = (e or [0, 0])
        # A state some fighter escaped from is not one the game refuses.
        return (ok + 1.0 + min(seen, 5) * 0.5) / (n + 2.0 + min(seen, 5) * 0.5)

    def res_ok(self, what, act):
        """Chance a `what` ('pu' / 'rising') pressed during `act` comes out,
        or None. Real starts from that act by any fighter count as evidence."""
        e = (self.data.get("res_ok") or {}).get(f"{what}|{act}")
        seen = (self.data.get("res_from") or {}).get(f"{what}|{act}", 0)
        if not e and not seen:
            return None
        n, ok = (e or [0, 0])
        return (ok + 1.0 + min(seen, 10) * 0.5) / (n + 2.0 + min(seen, 10) * 0.5)


_CACHE = {}     # path -> {"mtime", "obj"}: one per model folder in play


def get(path=None):
    """The table on disk (reloaded when the file changes), or an empty one.
    Default: the active model folder's copy (ajbot.datadir)."""
    from . import datadir
    p = Path(path) if path else datadir.path("threat.json")
    c = _CACHE.setdefault(str(p), {"mtime": None, "obj": Threat()})
    try:
        m = p.stat().st_mtime
    except OSError:
        return c["obj"] if c["mtime"] is None else Threat()
    if c["mtime"] != m:
        try:
            c["obj"] = Threat(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            c["obj"] = Threat()
        c["mtime"] = m
    return c["obj"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=None)
    ap.add_argument("--max-files", type=int, default=0, help="0 = all (per-file results are cached)")
    ap.add_argument("--show", default=None, help="print one character's contact table")
    a = ap.parse_args()
    if a.show:
        t = get()
        for act, node in sorted((t.contact.get(a.show) or {}).items(), key=lambda kv: -kv[1]["n"]):
            if node["n"] < 5:
                continue
            ps = " ".join(f"{t.p_contact(a.show, act, DIST_EDGES[b] + 1):.2f}"
                          for b in range(len(DIST_EDGES)))
            ct = t.contact_time(a.show, act, 300.0)
            print(f"{act:28} n={node['n']:5} touch={node['c'] / node['n']:.2f}  by dist {ps}"
                  + (f"  t@300={ct[1]:.2f}s" if ct else ""))
        return
    files = sorted(glob.glob(a.runs or "runs/*.jsonl")) + ([] if a.runs else sorted(glob.glob("botruns/*.jsonl")))
    print(f"mining {len(files)} recordings (up to {a.max_files or 'all'})")
    out = mine(files, a.max_files or None)
    from . import datadir
    dest = datadir.out("threat.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out), encoding="utf-8")
    nact = sum(len(v) for v in out["contact"].values())
    print(f"wrote {dest}: {nact} (character, act) pairs from {out['files']} recordings, "
          f"{len(out['combo'])} combo cells, {sum(v[0] for v in out['esc_ok'].values())} "
          f"escape presses, {sum(v[0] for v in out['res_ok'].values())} resource presses")


if __name__ == "__main__":
    main()
