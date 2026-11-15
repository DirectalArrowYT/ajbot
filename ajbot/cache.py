"""Per-file feature cache.

Re-parsing every recording on each training run does not scale: 41 matches is
already ~530 MB of JSON, and the plan is to record many more against random
opponents. Featurised arrays are tiny by comparison, so each recording is
converted once and cached.

A cache entry is invalidated by the recording's size and mtime, and by a
fingerprint of the feature definition - so changing ME_FIELDS or adding a
feature rebuilds automatically rather than silently mixing layouts (which is
what produced the "186 features but expecting 148" failure).
"""
import hashlib
import json
from pathlib import Path

import numpy as np

from . import tickcache

CACHE_DIR = Path("cache")


def feature_fingerprint(actions, chars):
    """Identifies the feature layout this cache entry was built with."""
    from . import dataset as D
    from . import moveinfo
    blob = json.dumps({
        "me": D.ME_FIELDS,
        "en": D.ENEMY_FIELDS,
        "scale": D.SCALE,
        "actions": list(actions),
        "chars": list(chars),
        "max_age": D.MAX_AGE,
        "version": D.FEATURE_VERSION,
        "acts": list(D.ACT_CLASS_NAMES),
        "reward": D.REWARD,
        "horizon": D.HORIZON_S,
        # The move table (moveinfo) is deliberately NOT part of the key: it is
        # re-mined from every new recording, and keying on it rebuilt all 550+
        # recordings' features on every retrain. Reach features in older cache
        # entries lag the table slightly; `retrain.bat --fresh` rebuilds all.
        "extra": D.EXTRA_N,
        # The route book pays route_step/route_done; it changes only when the
        # human recordings it is mined from do, so rebuilds stay rare.
        "routes": D.route_fingerprint(),
    }, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _entry(path, fp, kind):
    src = Path(path)
    size, mtime = tickcache.src_stat(src)
    key = hashlib.sha1(
        f"{src.parent.name}/{src.name}|{size}|{mtime}|{fp}|{kind}".encode()
    ).hexdigest()[:20]
    return CACHE_DIR / f"{src.stem}.{kind}.{key}.npz"


def load_or_build(path, actions, chars, kind, build_fn):
    """Return (X, y, w) for one recording, building and caching on first use.

    `build_fn(ticks)` produces (X, y) or (X, y, w); it is only called on a
    miss. `w` is the per-sample reward weight, all ones when not supplied.
    """
    fp = feature_fingerprint(actions, chars)
    out = _entry(path, fp, kind)
    if out.exists():
        with np.load(out) as z:
            X, y = z["X"], z["y"]
            w = z["w"] if "w" in z.files else np.ones(len(X), np.float32)
            return X, y, w

    from . import dataset as D
    ticks = D.load(path)
    res = build_fn(ticks)
    X, y = res[0], res[1]
    w = res[2] if len(res) > 2 else np.ones(len(X), np.float32)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Stale entries for this recording under a different layout are useless.
    for old in CACHE_DIR.glob(f"{Path(path).stem}.{kind}.*.npz"):
        if old != out:
            old.unlink(missing_ok=True)
    np.savez_compressed(out, X=X, y=y, w=w)
    return X, y, w


def vocab_counts(path):
    """Cached action/character counts for one recording.

    The vocabulary has to be known before features can be built, and deriving
    it needed a full load of every file - which defeated the point of caching
    the features. Counts are small, so they are cached separately and merged.
    """
    src = Path(path)
    size, mtime = tickcache.src_stat(src)
    key = hashlib.sha1(
        f"{src.parent.name}/{src.name}|{size}|{mtime}|vocab5".encode()
    ).hexdigest()[:20]
    out = CACHE_DIR / f"{src.stem}.vocab.{key}.json"
    if out.exists():
        d = json.loads(out.read_text(encoding="utf-8"))
        return d["acts"], d["chars"]

    from . import dataset as D
    acts, chars = D.vocab_counts(D.load(path))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for old in CACHE_DIR.glob(f"{src.stem}.vocab.*.json"):
        if old != out:
            old.unlink(missing_ok=True)
    out.write_text(json.dumps({"acts": acts, "chars": chars}), encoding="utf-8")
    return acts, chars


def merged_vocab(paths):
    """(actions, characters) across recordings, most frequent first."""
    acts, chars = {}, {}
    for p in paths:
        a, c = vocab_counts(p)
        for k, v in a.items():
            acts[k] = acts.get(k, 0) + v
        for k, v in c.items():
            chars[k] = chars.get(k, 0) + v
    order = lambda d: [k for k, _ in sorted(d.items(), key=lambda kv: -kv[1])]
    return order(acts), order(chars)


def clear():
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("*.npz"):
            f.unlink(missing_ok=True)
