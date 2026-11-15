"""Log characters the sampler cannot name, so they can be integrated.

22 of 113 characters ship no `Pl<NN><Name>` namespace in their Lua state, so
the sampler used to report them as id='?' and everything downstream keyed them
by a throwaway slot handle ('c152'). The sampler now also reads identity from
the character's own animation names ("EPl21DekuShootAnimationId::..."), which
names most of them once they play a character-specific move.

This writes logs/unknown_chars.jsonl - one line per event, once per session:

  unknown     a fighter with no name at all yet. Carries the slot, side, what
              it was doing, and - when the team was picked with --pick - which
              picked names were NOT matched by any fighter, i.e. who it
              probably is.
  resolved    a previously unknown fighter named by its animations: slot, the
              name found, the animation string that gave it away.
  unmapped    a named fighter whose name matches no entry in roster.py, so
              --pick cannot select it and name pins cannot reach it. The
              roster list needs this name added (or an alias).

Every line is self-contained JSON, so the file can simply be sent over or
grepped. It is appended to across sessions.
"""
import json
import time
from pathlib import Path

from .roster import DISPLAY_ORDER, FORMS, base_name

LOG_PATH = Path("logs") / "unknown_chars.jsonl"


def roster_name(cid):
    """The roster.py entry a Lua id belongs to, or None.

    'Pl02Bakugo' -> 'Bakugo', and alternate forms to their base character
    ('Pl247DabiFullBodyBurn' -> 'DabiFinal') via roster.FORMS.
    """
    return base_name(cid)


def _acts(rec):
    """True once a state has run a real action. Companion and helper states
    (a partner like Rappa's, summons, projectiles) never do: act stays 'None'
    and cmd 'None' for the whole match, and no animation will ever name them.
    They are not fighters, so they are counted, not logged one by one."""
    return (rec.get("act") not in (None, "None")
            or rec.get("cmd") not in (None, "None"))


class UnknownLog:
    def __init__(self, path=LOG_PATH, expected=None, source="play", quiet=False):
        self.path = Path(path)
        self.expected = list(expected or [])     # --pick names, if any
        self.source = source
        self.quiet = quiet
        self.seen = set()
        self.slot_state = {}      # raw slot -> last idsrc
        self.counts = {"unknown": 0, "resolved": 0, "unmapped": 0, "form": 0}
        self.companions = set()
        self._fh = None

    def _write(self, kind, entry):
        entry = {"event": kind, "t": round(time.time(), 3),
                 "source": self.source, **entry}
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(json.dumps(entry) + "\n")
        self._fh.flush()
        self.counts[kind] += 1
        if not self.quiet:
            what = entry.get("id") or entry.get("slot")
            extra = f" via {entry['anim']}" if entry.get("anim") else ""
            print(f"  [unknown-log] {kind}: {what}{extra}")

    def observe(self, snap, teams=None):
        """Check one raw snapshot (slot -> record) for anything worth logging."""
        side_of = {}
        for side in ("mine", "theirs"):
            for r in (teams or {}).get(side) or []:
                for k in (r.get("key"), r.get("id")):
                    if k:
                        side_of[k] = side
        named = {r.get("id") for r in snap.values()
                 if r.get("age", 1e9) <= 800 and r.get("id") not in (None, "?")}
        missing = [n for n in self.expected
                   if not any(roster_name(i) == n for i in named)]

        for slot, rec in snap.items():
            if rec.get("age", 1e9) > 800:
                continue
            cid = rec.get("id")
            src = rec.get("idsrc")
            prev = self.slot_state.get(slot)
            self.slot_state[slot] = src

            if src == "none" or cid == "?":
                if not _acts(rec):
                    self.companions.add(slot)
                    continue
                if ("unknown", slot) not in self.seen:
                    self.seen.add(("unknown", slot))
                    self._write("unknown", {
                        "slot": slot,
                        "side": side_of.get(slot, "?"),
                        "enemy_flag": rec.get("enemy"), "ai": rec.get("ai"),
                        "active": rec.get("active"), "sidekick": rec.get("sk"),
                        "hp": rec.get("hp"), "cmd": rec.get("cmd"),
                        "act": rec.get("act"), "anim": rec.get("anim"),
                        "named_on_field": sorted(named),
                        "picked_but_unmatched": missing,
                    })
                continue

            if src == "anim" and ("resolved", slot, cid) not in self.seen:
                self.seen.add(("resolved", slot, cid))
                self._write("resolved", {
                    "slot": slot, "id": cid, "anim": rec.get("anim"),
                    "was_unknown": prev == "none",
                    "roster_name": roster_name(cid),
                    "side": side_of.get(cid, "?"),
                })

            name = cid[2:].lstrip("0123456789") if cid and cid.startswith("Pl") else None
            if name in FORMS and ("form", cid) not in self.seen:
                self.seen.add(("form", cid))
                self.counts["form"] += 1      # known alternate form: no log line
            if cid and cid.startswith("Pl") and roster_name(cid) is None \
                    and ("unmapped", cid) not in self.seen:
                self.seen.add(("unmapped", cid))
                self._write("unmapped", {
                    "id": cid, "slot": slot, "idsrc": src or "namespace",
                    "side": side_of.get(cid, "?"),
                })

    def summary(self):
        c = self.counts
        if not any(c.values()) and not self.companions:
            return None
        return (f"unknown-character log: {c['unknown']} unnamed fighters, "
                f"{c['resolved']} named from animations, {c['form']} known "
                f"alternate forms, {len(self.companions)} companion states "
                f"ignored, {c['unmapped']} not in roster.py -> {self.path}")

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


PROJ_LOG_PATH = Path("logs") / "unknown_projectiles.jsonl"


class UnknownProjectileLog:
    """Logs projectile names live play turns up that sampler.PROJ_NAMES does
    not know about yet - see the Projectile.GetOwnedProjectileCount/CheckOwned
    /CheckDeadOwned pass-through wrap in sampler.RESIDENT, which records every
    string name any character's own script actually passes into A.proj_seen.

    Unlike UnknownLog.observe(), poll() costs one extra hook.eval() call (a
    one-shot query lands on one random lua_State, same as everything else
    built on Hook.eval), so call it every few seconds from the main loop, not
    every tick.

    This writes logs/unknown_projectiles.jsonl - one line per newly-seen name,
    appended across sessions, so the ajbot.sampler.PROJ_NAMES table can be
    grown from what real play actually turns up.
    """

    def __init__(self, path=PROJ_LOG_PATH, source="record", quiet=False):
        self.path = Path(path)
        self.source = source
        self.quiet = quiet
        self.seen = set()
        self.count = 0
        self._fh = None

    def poll(self, hook):
        from . import sampler
        try:
            out = hook.eval(sampler._UNKNOWN_PROJ_QUERY)
        except Exception:
            return
        if not out:
            return
        import re
        for line in out.splitlines():
            if "|" not in line or line in self.seen:
                continue
            cid, name = line.split("|", 1)
            m = re.match(r"^(Pl\d+)", cid)
            pid = m.group(1) if m else cid
            if name in sampler.PROJ_NAMES.get(pid, ()):
                continue
            self.seen.add(line)
            self._write({"id": cid, "name": name})

    def _write(self, entry):
        entry = {"event": "unknown_projectile", "t": round(time.time(), 3),
                 "source": self.source, **entry}
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(json.dumps(entry) + "\n")
        self._fh.flush()
        self.count += 1
        if not self.quiet:
            print(f"  [unknown-log] projectile: {entry['id']} -> {entry['name']}")

    def summary(self):
        if not self.count:
            return None
        return f"unknown-projectile log: {self.count} new name(s) -> {self.path}"

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


ACTION_LOG_PATH = Path("logs") / "unknown_actions.jsonl"


class UnknownActionLog:
    """Logs `act` strings sampler.act_class() cannot place in any of
    ACT_CLASSES - it falls back to 'other' for them, silently, and nothing
    else in the pipeline ever flags that. This is exactly the shape of gap
    that let AttackDamageEscape go unlabelled: it fed a real event counter
    (ev_esc) and a real reward (REWARD["ev_esc"]) the whole time, but
    DEFENCE_LABELS never turned it into a trainable decision because no
    label_of() branch was watching for it, and nothing was logging "other"
    acts to make that omission visible.

    observe() reads the raw per-tick snapshot already being read every tick
    elsewhere (the same `snap` passed to UnknownLog.observe()) - no extra
    hook calls, unlike UnknownProjectileLog.

    Expect noise: plenty of 'other' acts are perfectly mundane (companion/
    summon states, transitional animation names) and don't need their own
    tactical category. This is a candidate list to review, not a to-do list -
    logs/unknown_actions.jsonl is for spotting the ones that DO matter, the
    way AttackDamageEscape did.
    """

    def __init__(self, path=ACTION_LOG_PATH, source="record", quiet=False):
        self.path = Path(path)
        self.source = source
        self.quiet = quiet
        self.seen = set()
        self.count = 0
        self._fh = None

    def observe(self, snap):
        from .sampler import act_class, KNOWN_OTHER_ACTS
        for rec in snap.values():
            act = rec.get("act")
            if not act or act == "None" or act in self.seen or act in KNOWN_OTHER_ACTS:
                continue
            if act_class(act) != "other":
                continue
            self.seen.add(act)
            self._write({"act": act})

    def _write(self, entry):
        entry = {"event": "unknown_action", "t": round(time.time(), 3),
                 "source": self.source, **entry}
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(json.dumps(entry) + "\n")
        self._fh.flush()
        self.count += 1
        if not self.quiet:
            print(f"  [unknown-log] action: {entry['act']}")

    def summary(self):
        if not self.count:
            return None
        return f"unknown-action log: {self.count} new act string(s) -> {self.path}"

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None
