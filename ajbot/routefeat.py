"""Where the controlled fighter is in a known combo route, as policy features.

The route follower (ajbot.routes.RouteFollower) plays routes by rule. These
features only TELL the model what the routes say - how deep the moves so far
match a known route, how much of it is left, which step the matching routes
take next, and whether the combo is still landing - and the model learns
from the recordings when that is worth following. Nothing here presses a
button.

The book is fixed when a policy is trained (train.py stores it in the policy
as "route_book") so live play matches the routes the policy learned with,
whatever the data folder holds by then.
"""
import collections

# The next-step distribution is over this fixed list (route steps outside it
# count as "other"), so the block has the same size for every policy.
LABELS = ["Melee", "ExMelee", "Unique1", "Unique2", "ExUnique1", "ExUnique2",
          "Dash", "DashForward", "Jump", "Unguardable", "CounterAttack",
          "PlusUltra1", "ExtremeAction", "Dodge", "UniqueCharge1",
          "ExUnique2FollowX", "ExUnique2FollowY", "ExUnique2FollowA"]
_LIDX = {l: i for i, l in enumerate(LABELS)}
FIXED = ["route.depth", "route.matched", "route.left", "route.routes", "route.live"]
FEATURE_NAMES = FIXED + [f"route.next={l}" for l in LABELS] + ["route.next=other"]
N_FEATS = len(FEATURE_NAMES)

NOISE = {None, "None", "Guard", "JustGuard"}
GAP_S = 2.5        # RouteFollower's engagement gap: a pause this long ends the combo
LIVE_S = 1.0       # the combo is "landing" if the opponent lost health this recently
MAX_DEPTH = 32


class Index:
    """{char: {prefix tuple: (Counter(next step), min steps left)}}."""

    def __init__(self, book):
        self.book = {c: [tuple(q) for q in v] for c, v in (book or {}).items()}
        self.pref = {}
        for c, seqs in self.book.items():
            d = self.pref.setdefault(c, {})
            for q in seqs:
                for k in range(1, len(q)):
                    nxt, left = d.get(q[:k], (None, None))
                    if nxt is None:
                        nxt, left = collections.Counter(), len(q) - k
                    nxt[q[k]] += 1
                    d[q[:k]] = (nxt, min(left, len(q) - k))
        self.maxlen = min(MAX_DEPTH, max((len(q) for v in self.book.values() for q in v), default=0))

    def match(self, char, toks):
        """(depth, Counter(next), steps left) of the deepest route prefix the
        last moves `toks` match, or (0, None, 0)."""
        d = self.pref.get(char)
        if not d:
            return 0, None, 0
        for k in range(min(len(toks), self.maxlen), 0, -1):
            hit = d.get(tuple(toks[-k:]))
            if hit:
                return k, hit[0], hit[1]
        return 0, None, 0


class RouteTrack:
    """Per side: follows one fighter's label stream (same labels as training)
    and the opponent's health, and turns them into the route features."""

    def __init__(self, index):
        self.index = index
        self.hist, self.key, self.prev = [], None, None
        self.en_key, self.en_hp, self.dealt = None, None, -1e9
        self.now = 0.0

    def observe(self, me, en, now):
        from .dataset import label_of
        me, en = me or {}, en or {}
        key = me.get("key")
        lab = label_of(self.prev, me) if (self.prev and me and self.prev.get("key") == key) else None
        self.prev = me or None
        if key != self.key:
            self.hist, self.key = [], key
        if lab not in NOISE:
            if self.hist and now - self.hist[-1][0] > GAP_S:
                self.hist = []
            self.hist.append((now, lab))
            del self.hist[:-MAX_DEPTH]
        h, ek = en.get("hp"), en.get("key")
        if isinstance(h, (int, float)):
            if ek == self.en_key and self.en_hp is not None and h < self.en_hp - 1e-4:
                self.dealt = now
            self.en_key, self.en_hp = ek, h
        self.now = now

    def feats(self, me):
        from .roster import move_char
        out = [0.0] * N_FEATS
        if self.index is None:
            return out
        out[4] = 1.0 if self.now - self.dealt <= LIVE_S else 0.0
        if not self.hist or self.now - self.hist[-1][0] > GAP_S:
            return out
        depth, nxt, left = self.index.match(move_char(me or {}), [l for _, l in self.hist])
        if not depth:
            return out
        tot = sum(nxt.values())
        out[0] = min(depth, 20) / 10.0
        out[1] = 1.0
        out[2] = min(left, 20) / 10.0
        out[3] = min(tot, 20) / 10.0
        base = len(FIXED)
        for lab, n in nxt.items():
            out[base + _LIDX.get(lab, len(LABELS))] += n / tot
        return out
