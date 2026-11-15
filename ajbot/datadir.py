"""Which model folder's data files a bot reads: combo book, move and threat
stats. Each policy carries its own copies next to its policy.pkl, so the
champion is a frozen snapshot and a challenger plays with what it was
trained with, instead of both sides sharing whatever models/ holds now.

    with datadir.use("models/challenger"):   # this bot's decisions
        moveinfo.get()                       # models/challenger/moveinfo.json

A file missing from the active folder falls back to models/. AJBOT_DATA_DIR
sets the starting folder (the league's training chain writes into
models/fit this way, never over the champion's models/).
"""
import contextlib
import os
from pathlib import Path

DEFAULT = Path("models")
_stack = [Path(os.environ.get("AJBOT_DATA_DIR") or DEFAULT)]


def active():
    return _stack[-1]


def path(name):
    """Where to read `name`: the active folder's copy, else models/."""
    p = _stack[-1] / name
    return p if p.exists() else DEFAULT / name


def path_in(folder, name):
    """`name` in `folder`, else models/."""
    p = Path(folder) / name
    return p if p.exists() else DEFAULT / name


def out(name):
    """Where a builder writes `name`: always the active folder."""
    return _stack[-1] / name


@contextlib.contextmanager
def use(folder):
    _stack.append(Path(folder))
    try:
        yield
    finally:
        _stack.pop()
