"""Keep recordings trainable while they are being written.

Every recording train.py rejected as "frozen" (tools/archive_junk.py) held no
fighting at all: a file opened on a victory pose or a paused/stuck screen and
logged the same still picture for minutes (one: 270 s of WinDemo, 8013 ticks).
Nothing in such a file can be repaired afterwards, so this stops it being
written in the first place:

  - nothing is written until the fight is actually moving (the last
    PREROLL ticks before that are kept, so the start is not cut);
  - once nothing has moved for FREEZE_S, writing pauses until it moves again
    (the still stretch is dropped, the last PREROLL ticks of it kept);
  - a file that never saw MIN_PLAY moving ticks is deleted on close instead
    of being left for the trainer to reject.

"Moving" = some fresh fighter's act or health changed since the previous
tick, and the fighter on point is not in a demo pose. Not act_n: it keeps
counting on the result screen while the fighters lie there. At 15 Hz an act
lasts several ticks, so a real match only shows a change every few ticks -
MIN_PLAY is a count of changes, not of ticks.
"""
import collections
import json
from pathlib import Path

DEMO_ACTS = {None, "None", "AppearanceDemo", "WinDemo"}
PREROLL = 45
FREEZE_S = 8.0
MIN_PLAY = 12
FRESH_AGE = 60


def _signature(rec):
    chars = rec.get("chars") or {}
    return tuple(sorted(
        (k, r.get("act"),
         round(r["hp"], 3) if isinstance(r.get("hp"), (int, float)) else None)
        for k, r in chars.items() if (r.get("age") or 0) <= FRESH_AGE))


class RecGuard:
    """Wraps an open recording file. write_rec(dict) for ticks, write(str)
    for anything else (summary lines), close() -> True if the file was kept."""

    def __init__(self, fh, path):
        self.fh, self.path = fh, Path(path)
        self.buf = collections.deque(maxlen=PREROLL)
        self.sig = None
        self.started = False
        self.paused = False
        self.last_move_t = None
        self.play = 0          # ticks where something changed
        self.written = 0
        self.dropped = 0
        self.closed = False

    def _moving(self, rec):
        sig = _signature(rec)
        changed = self.sig is not None and sig != self.sig
        self.sig = sig
        me = (rec.get("chars") or {}).get(rec.get("me")) or {}
        return changed and me.get("act") not in DEMO_ACTS

    def _put(self, rec):
        self.fh.write(json.dumps(rec) + "\n")
        self.written += 1

    def write_rec(self, rec):
        t = rec.get("t") or 0.0
        if self._moving(rec):
            self.play += 1
            self.last_move_t = t
            if not self.started or self.paused:
                if self.paused:
                    print(f"  recording: moving again - writing resumed "
                          f"({self.path.name})", flush=True)
                self.started, self.paused = True, False
                while self.buf:
                    self._put(self.buf.popleft())
            self._put(rec)
            return
        if self.started and not self.paused and t - (self.last_move_t or t) < FREEZE_S:
            self._put(rec)
            return
        if self.started and not self.paused:
            self.paused = True
            print(f"  recording: nothing has moved for {FREEZE_S:.0f}s - not "
                  f"writing until it does ({self.path.name})", flush=True)
        if len(self.buf) == self.buf.maxlen:
            self.dropped += 1
        self.buf.append(rec)

    def write(self, line):
        self.fh.write(line)

    def close(self):
        """Close; delete the file if it never recorded play. True if kept."""
        if self.closed:
            return self.play >= MIN_PLAY
        self.closed = True
        self.fh.close()
        if self.play >= MIN_PLAY:
            if self.dropped > 0:
                print(f"  recording: left out {self.dropped} still ticks "
                      f"({self.path.name})", flush=True)
            return True
        try:
            self.path.unlink()
        except OSError:
            pass
        print(f"  recording discarded: {self.path.name} never showed any "
              f"fighting ({self.play} moving ticks) - it would have been "
              f"unusable for training", flush=True)
        return False
