"""What this opponent tends to do, learned during the match.

The policy used to see one moment of the match and nothing about the person
or bot across from it, so it played every opponent identically. This keeps
decaying statistics of the opposing side over the match and turns them into
features (appended to dataset.featurize):

  attack_rate   opponent attack starts per second
  guard/jump/dash/idle   share of the opponent's free time spent in each
  attack_dist   distance the opponent likes to start attacks from (/1000)
  guards_mine   how often they guard within 0.5 s of OUR attack start
  counters_mine how often they just-guard / counter / escape our attacks
  my_hit        share of our attack starts that connected
  their_hit     share of their attack starts that connected on us
  seen          confidence, 0..1 by how much has been observed

It also times each side's current act (when it began), which turns the
game's own frame data (ajbot.framedata: startup, hit window, recovery) into
"how long until their move hits" and "how long they are stuck" - see
framedata.features, appended after the sequence values.

Everything is updated causally - an outcome is only counted once the time
it needs has actually passed - so training (dataset.build_*) and live play
(agent.Agent.decide) see exactly the same numbers at the same moment.
"""
import math

ATTACK_CLASSES = ("attack", "combo", "unblock", "pu")
HALF_LIFE_S = 45.0          # older behaviour fades: people change mid-match
HIT_WINDOW_S = 1.0
REACT_WINDOW_S = 0.6
RESET_GAP_S = 20.0          # nothing observed this long: a new match
SEQ_WINDOW_S = 3.0          # recent-sequence memory
SEQ_TAU_S = 1.0             # recency weighting inside it

# Coarse move groups for the recent-sequence features: fixed, so the feature
# layout does not depend on a policy's action vocabulary.
GROUPS = ["Melee", "ExMelee", "Unique1", "Unique2", "ExUnique1", "ExUnique2",
          "Unguardable", "PlusUltra", "Guard", "Evade", "Jump", "Dash",
          "Counter", "Other"]


def group_of(label):
    if not label:
        return None
    if label.startswith("ExUnique2"):
        return "ExUnique2"
    if label in GROUPS:
        return label
    if label.startswith("PlusUltra") or label.startswith("SuperPlusUltra"):
        return "PlusUltra"
    if label in ("Guard", "JustGuard"):
        return "Guard"
    if label in ("Dodge", "Escape", "DamageEscape", "DashBack"):
        return "Evade"
    if label in ("Dash", "DashForward"):
        return "Dash"
    if label == "CounterAttack":
        return "Counter"
    if label.startswith("Unique"):
        return "Unique1" if "1" in label else "Unique2"
    return "Other"


N_SEQ = 2 * len(GROUPS) + 2
from .framedata import N_FEATS as _FD_N, FEATURE_NAMES as _FD_NAMES   # noqa: E402
N_FEATS = 11 + N_SEQ + _FD_N


def _cls(rec):
    from .sampler import act_class
    return act_class((rec or {}).get("act")) if rec and "act" in rec else None


class _EMA:
    __slots__ = ("v", "w")

    def __init__(self):
        self.v, self.w = 0.0, 0.0

    def add(self, x, decay):
        self.v = self.v * decay + x
        self.w = self.w * decay + 1.0

    def decay(self, d):
        self.v *= d
        self.w *= d

    def mean(self, default=0.0):
        return self.v / self.w if self.w > 1e-6 else default


class OpponentModel:
    def __init__(self):
        self.reset()

    def reset(self):
        self.t = None
        self.attacks = 0.0           # decayed count of opponent attack starts
        self.time = 0.0              # decayed seconds observed
        self.state = {k: _EMA() for k in ("guard", "jump", "dash", "idle")}
        self.attack_dist = _EMA()
        self.guards_mine = _EMA()
        self.counters_mine = _EMA()
        self.my_hit = _EMA()
        self.their_hit = _EMA()
        self.events = 0
        self.prev_me = None
        self.prev_en = None
        self.pending = []            # (kind, t0, enemy_hp0 / my_hp0 / counters0)
        self.seq = {"me": [], "en": []}   # (t, group) recent decisions per side
        self.last_me_t = None
        self.last_en_att_t = None
        self.act_t0 = {"me": None, "en": None}
        self.cur = {"me": None, "en": None}
        self.now = None

    # ------------------------------------------------------------------ update
    def update(self, me, en, t):
        """Feed one tick (our record, the opponent's record, time in seconds)."""
        if not me or not en or t is None:
            return
        if self.t is not None and (t - self.t > RESET_GAP_S or t < self.t):
            self.reset()
        self._time_acts(me, en, t)
        if self.t is None:
            self.t = t
            self.prev_me, self.prev_en = me, en
            return
        dt = max(0.0, min(1.0, t - self.t))
        self.t = t
        d = 0.5 ** (dt / HALF_LIFE_S)
        self.attacks *= d
        self.time = self.time * d + dt
        ec = _cls(en)
        free = ec not in ATTACK_CLASSES and ec not in ("damage", "recovered") and ec is not None
        if free:
            a = en.get("act") or ""
            for k, hit in (("guard", ec == "guard"),
                           ("jump", a.startswith(("Jump", "AirJump", "DashJump", "Fall"))),
                           ("dash", a.startswith(("Dash", "Run", "AirDash", "Parkour"))),
                           ("idle", a in ("Idle", "None", "Wait", "Stand"))):
                self.state[k].add(1.0 if hit else 0.0, d)

        from .dataset import label_of
        from . import moveinfo
        en_lab = label_of(self.prev_en, en)
        me_lab = label_of(self.prev_me, me)
        for side, lab in (("me", me_lab), ("en", en_lab)):
            g = group_of(lab)
            if g:
                self.seq[side].append((t, g))
                self.seq[side] = [(tt, gg) for tt, gg in self.seq[side] if t - tt <= SEQ_WINDOW_S]
                if side == "me":
                    self.last_me_t = t
        self.now = t
        if en_lab and en_lab not in moveinfo.NON_ATTACK and ec in ATTACK_CLASSES:
            self.attacks += 1.0
            self.events += 1
            dist = me.get("dist")
            if isinstance(dist, (int, float)) and dist < 20000:
                self.attack_dist.add(dist / 1000.0, d)
            self.pending.append(("theirs", t, me.get("hp")))
            self.last_en_att_t = t
        if me_lab and me_lab not in moveinfo.NON_ATTACK and _cls(me) in ATTACK_CLASSES:
            self.events += 1
            self.pending.append(("mine", t, en.get("hp"),
                                 (en.get("ev_jg") or 0) + (en.get("ev_ctr") or 0) + (en.get("ev_esc") or 0)))
            self.pending.append(("react", t, None))
        self._resolve(me, en, t, d)
        self.prev_me, self.prev_en = me, en

    def _time_acts(self, me, en, t):
        for side, rec, prev in (("me", me, self.prev_me), ("en", en, self.prev_en)):
            if (prev is None or self.act_t0[side] is None
                    or rec.get("key") != prev.get("key") or rec.get("act") != prev.get("act")
                    or (rec.get("act_n") or 0) < (prev.get("act_n") or 0)):
                self.act_t0[side] = t
            self.cur[side] = rec
        self.now = t

    def _resolve(self, me, en, t, d):
        keep = []
        for p in self.pending:
            kind, t0 = p[0], p[1]
            age = t - t0
            if kind == "react":
                if _cls(en) == "guard":
                    self.guards_mine.add(1.0, d)
                    continue
                if age >= REACT_WINDOW_S:
                    self.guards_mine.add(0.0, d)
                    continue
            elif kind == "mine":
                hp0 = p[2]
                hp = en.get("hp")
                if isinstance(hp0, (int, float)) and isinstance(hp, (int, float)) and hp0 - hp > 0.004:
                    self.my_hit.add(1.0, d)
                    self.counters_mine.add(0.0, d)
                    continue
                c = (en.get("ev_jg") or 0) + (en.get("ev_ctr") or 0) + (en.get("ev_esc") or 0)
                if c > p[3]:
                    self.counters_mine.add(1.0, d)
                    self.my_hit.add(0.0, d)
                    continue
                if age >= HIT_WINDOW_S:
                    self.my_hit.add(0.0, d)
                    self.counters_mine.add(0.0, d)
                    continue
            elif kind == "theirs":
                hp0 = p[2]
                hp = me.get("hp")
                if isinstance(hp0, (int, float)) and isinstance(hp, (int, float)) and hp0 - hp > 0.004:
                    self.their_hit.add(1.0, d)
                    continue
                if age >= HIT_WINDOW_S:
                    self.their_hit.add(0.0, d)
                    continue
            keep.append(p)
        self.pending = keep[-40:]

    # ----------------------------------------------------------------- features
    def feats(self):
        rate = self.attacks / self.time if self.time > 1.0 else 0.0
        return self._habit_feats(rate) + self._seq_feats() + self._frame_feats()

    def _frame_feats(self):
        from .framedata import features
        dt = {s: (None if self.act_t0[s] is None or self.now is None else self.now - self.act_t0[s])
              for s in ("me", "en")}
        return features(self.cur["me"], self.cur["en"], dt["me"], dt["en"])

    def _seq_feats(self):
        t = getattr(self, "now", None)
        out = []
        for side in ("me", "en"):
            w = dict.fromkeys(GROUPS, 0.0)
            for tt, g in self.seq[side]:
                if t is not None and t - tt <= SEQ_WINDOW_S:
                    w[g] += math.exp(-(t - tt) / SEQ_TAU_S)
            out += [min(w[g], 3.0) / 3.0 for g in GROUPS]
        for last in (self.last_me_t, self.last_en_att_t):
            out.append(1.0 if last is None or t is None else min(t - last, SEQ_WINDOW_S) / SEQ_WINDOW_S)
        return out

    def _habit_feats(self, rate):
        return [min(rate, 3.0) / 3.0,
                self.state["guard"].mean(), self.state["jump"].mean(),
                self.state["dash"].mean(), self.state["idle"].mean(),
                min(self.attack_dist.mean(), 5.0) / 5.0,
                self.guards_mine.mean(), self.counters_mine.mean(),
                self.my_hit.mean(), self.their_hit.mean(),
                min(1.0, self.events / 20.0)]


FEATURE_NAMES = (["opp.attack_rate", "opp.guard", "opp.jump", "opp.dash", "opp.idle",
                  "opp.attack_dist", "opp.guards_mine", "opp.counters_mine",
                  "opp.my_hit", "opp.their_hit", "opp.seen"]
                 + [f"seq.me.{g}" for g in GROUPS] + [f"seq.en.{g}" for g in GROUPS]
                 + ["seq.me_since_action", "seq.en_since_attack"] + _FD_NAMES)
assert len(FEATURE_NAMES) == N_FEATS
