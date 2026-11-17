"""Log actions nobody has told the pad how to press yet.

ACTION_MAP in agent.py is hand-written: someone confirmed which buttons a
move actually needs. Anything the model calls for, or the game's own `cmd`
names, outside that map gets silently dropped in `plan_press`. This writes
logs/unknown_actions.jsonl instead - one line per action name per session -
so the gap is visible, and `learn_actions.py` has a record of what showed up
in play but was never resolved automatically.
"""
import json
import time
from pathlib import Path

LOG_PATH = Path("logs") / "unknown_actions.jsonl"


class UnknownActionLog:
    def __init__(self, path=LOG_PATH, source="play", quiet=False):
        self.path = Path(path)
        self.source = source
        self.quiet = quiet
        self.seen = set()
        self._fh = None

    def observe(self, action, character=None, reason="not in ACTION_MAP"):
        """Note one decision that named `action` with no known button press."""
        if not action or action in self.seen:
            return
        self.seen.add(action)
        entry = {"event": "unmapped_action", "t": round(time.time(), 3),
                 "source": self.source, "action": action, "reason": reason,
                 "character": character}
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(json.dumps(entry) + "\n")
        self._fh.flush()
        if not self.quiet:
            print(f"  [unknown-action] {action!r} has no button mapping yet"
                  f"{f' ({character})' if character else ''} -> {self.path}")

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None
