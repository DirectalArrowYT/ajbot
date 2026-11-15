"""Machine-specific settings: training servers and game install paths.

Read from ajbot_local.json in the repo root (gitignored, never published).
Copy ajbot_local.example.json to start one. Without it there are no servers
and the game paths fall back to environment variables / auto-detection, which
is all a player who only runs the bot needs.

    {
      "servers": {"server": {"host": "user@host", "dir": "/path/AJBot",
                             "python": "/path/AJBot/venv/bin/python",
                             "linux": true, "lockstep": "/path/AJBot-lockstep"}},
      "game": {"dir": "...", "prefix": "...", "launcher": "..."}
    }
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = Path(os.environ.get("AJBOT_LOCAL", ROOT / "ajbot_local.json"))
_cfg = None


def load():
    global _cfg
    if _cfg is None:
        try:
            _cfg = json.loads(PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cfg = {}
    return _cfg


def get(dotted, default=None):
    """get("game.prefix") -> value, or default when unset."""
    cur = load()
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def servers():
    return dict(get("servers", {}) or {})


def server(name="server", required=True):
    """A server entry; exits with a pointer to the settings file if missing."""
    s = servers().get(name)
    if s is None and required:
        raise SystemExit(f"no '{name}' server in {PATH} "
                         f"(see ajbot_local.example.json)")
    return s
