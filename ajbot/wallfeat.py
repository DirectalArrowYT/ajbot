"""Which way the wall is, as policy features.

The game gives no position or stage size, only "touching a wall now"
(IsTouchWall, `twal`), so the model could not tell a wall behind it from one
between it and the opponent. Measured 2026-11-16: 76% of the bot's dashes
while touching a wall pushed straight at an opponent 450+ away - into
whatever stood in between - where people turn off it.

This remembers the stick direction (relative to the opponent, as
dataset._geometry_feats' lever_rel) at the moment of contact - the way that
hit the wall - and keeps it for a second after, so the model can learn from
the recordings which way not to push.
"""
import numpy as np

FEATURE_NAMES = ["wall.touch", "wall.recent", "wall.dir_sin", "wall.dir_cos", "wall.contacts"]
N_FEATS = len(FEATURE_NAMES)

KEEP_S = 1.0       # the contact direction stays this long after leaving the wall
COUNT_S = 3.0      # contacts counted over this window


class WallTrack:
    """Per side: follows one fighter's wall contacts."""

    def __init__(self):
        self.key = None
        self.touch = False
        self.last = -1e9          # last tick touching a wall
        self.dir = None           # (sin, cos) of the stick at the latest contact
        self.starts = []          # contact start times
        self.now = 0.0

    def observe(self, me, en, now):
        from .dataset import _geometry_feats
        me = me or {}
        key = me.get("key")
        if key != self.key:
            self.__init__()
            self.key = key
        touch = bool(me.get("twal"))
        if touch and not self.touch:
            self.starts.append(now)
            self.starts = [t for t in self.starts if now - t <= COUNT_S]
            if isinstance(me.get("lcam"), (int, float)):
                s, c = _geometry_feats(me, en)[:2]
                self.dir = (s, c)
        if touch:
            self.last = now
        self.touch, self.now = touch, now

    def feats(self):
        age = self.now - self.last
        if age > KEEP_S or self.dir is None:
            return [1.0 if self.touch else 0.0, 0.0, 0.0, 0.0,
                    sum(1 for t in self.starts if self.now - t <= COUNT_S) / 3.0]
        return [1.0 if self.touch else 0.0, float(np.clip(1.0 - age / KEEP_S, 0.0, 1.0)),
                self.dir[0], self.dir[1],
                sum(1 for t in self.starts if self.now - t <= COUNT_S) / 3.0]
