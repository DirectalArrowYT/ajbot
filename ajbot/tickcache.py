"""Compact digests of recordings, and per-file facts derived from them.

A recording is 10-40 MB of JSON lines; every tool in the retrain (junk check,
combo miner, combo book, move table, trainer) used to parse all 6 GB of them
again, each separately. The first time a recording is read it is stored here
as a zstd-compressed pickle of its parsed ticks (~20x smaller, ~3x faster to
load), and later reads use that. Small facts computed from a whole file
(junk verdict, opponent type, passivity, move-table counts...) are cached
next to it, so they are computed once per recording, not once per run.

Entries are keyed by the source file's size and modification time, so a
recording that changes is simply re-read. The original JSON is never touched.

    python -m ajbot.tickcache            # digest every recording now (parallel)
    python -m ajbot.tickcache --stats
"""
import argparse
import glob
import time
import hashlib
import json
import os
import pickle
from pathlib import Path

# Digests are zstd. Python 3.14 has it built in; older Pythons (the training
# server's 3.11 venv) need the zstandard package - the format must be the same
# everywhere, because digests made here are read there. 2026-11-06: a zlib
# fallback on the server could not read the synced zstd digests, and the
# delete-on-bad-read below wiped all 623 of them.
try:
    from compression import zstd as _z          # Python 3.14+
except ImportError:                             # pragma: no cover
    import io as _io
    import zstandard as _zs

    class _z:
        @staticmethod
        def compress(b):
            return _zs.ZstdCompressor(level=3).compress(b)

        @staticmethod
        def decompress(b):
            # stream: frames from compression.zstd may not record their size
            with _zs.ZstdDecompressor().stream_reader(_io.BytesIO(b)) as r:
                return r.read()

ROOT = Path("cache")
TICKS = ROOT / "ticks"
META = ROOT / "meta"
TICK_VERSION = 1            # bump if the stored form of ticks changes


def rec_id(path):
    """Machine-independent name of a recording: 'botruns/123.a.jsonl'. Cache
    keys use this rather than the absolute path, so caches built on one
    machine are valid on another (remote training on the laptop)."""
    p = Path(path)
    return f"{p.parent.name}/{p.name}"


# Stubs: on the training laptop a recording can be an empty placeholder whose
# real size and mtime are listed in cache/stubs.json. Every cache key is built
# from those, so the digests and facts made on the main PC are hits there and
# the 10-40 MB JSON never has to be copied (remote_retrain.py).
STUBS = ROOT / "stubs.json"
_STUB_TABLE = {"mtime": None, "rows": {}}


class StubMiss(RuntimeError):
    """A stub recording needed something that was not cached for it."""


def _stubs():
    try:
        m = STUBS.stat().st_mtime
    except OSError:
        return {}
    if _STUB_TABLE["mtime"] != m:
        try:
            _STUB_TABLE["rows"] = json.loads(STUBS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _STUB_TABLE["rows"] = {}
        _STUB_TABLE["mtime"] = m
    return _STUB_TABLE["rows"]


def src_stat(path):
    """(size, int mtime) of the recording - the real one for a stub."""
    st = Path(path).stat()
    if st.st_size == 0:
        row = _stubs().get(rec_id(path))
        if row:
            return int(row[0]), int(row[1])
    return st.st_size, int(st.st_mtime)


def is_stub(path):
    return Path(path).stat().st_size == 0 and rec_id(path) in _stubs()


def src_sizes(files):
    """Byte sizes for worker sizing (parallel.safe_worker_count)."""
    return [src_stat(f)[0] for f in files]


def _key(path, extra=""):
    size, mtime = src_stat(path)
    raw = f"{rec_id(path)}|{size}|{mtime}|{extra}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _old_key(path, extra=""):
    st = Path(path).stat()
    raw = f"{Path(path).resolve()}|{st.st_size}|{int(st.st_mtime)}|{extra}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def migrate_keys(files):
    """One-off: rename digests/facts keyed by absolute path to rec_id keys."""
    renamed = 0
    for f in files:
        name = Path(f).name
        for p in list(TICKS.glob(f"{name}.*.pkl.zst")):
            old = _old_key(f, f"v{TICK_VERSION}")
            if p.name == f"{name}.{old}.pkl.zst":
                p.rename(TICKS / f"{name}.{_key(f, f'v{TICK_VERSION}')}.pkl.zst")
                renamed += 1
        for p in list(META.glob(f"{name}.*")):
            rest = p.name[len(name) + 1:]          # fact.key.ext
            parts = rest.split(".")
            if len(parts) < 3:
                continue
            fact, key, ext = parts[0], parts[1], ".".join(parts[2:])
            extra = f"{fact}v1"
            if key == _old_key(f, extra):
                p.rename(META / f"{name}.{fact}.{_key(f, extra)}.{ext}")
                renamed += 1
    return renamed


def _write_atomic(dest, data):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


# A recording written to this recently is taken to be still recording.
LIVE_S = 120


def _parse(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    t = json.loads(line)
                except ValueError:
                    continue          # a half-written line: crash, or still recording
                if not t.get("summary"):
                    out.append(t)
    return out


def raw_ticks(path):
    """Parsed ticks of one recording (summary lines dropped), NOT stabilized."""
    stem = Path(path).name
    dest = _digest_path(path)
    if dest.exists():
        try:
            return pickle.loads(_z.decompress(dest.read_bytes()))
        except Exception:
            if is_stub(path):       # cannot be rebuilt here - keep it, say why
                raise StubMiss(f"{rec_id(path)}: digest unreadable here ({dest.name})")
            dest.unlink(missing_ok=True)
    if is_stub(path):
        raise StubMiss(f"{rec_id(path)}: stub without a digest - sync it again")
    ticks = _parse(path)
    try:
        _write_atomic(dest, _z.compress(pickle.dumps(ticks, protocol=5)))
        for old in TICKS.glob(f"{stem}.*.pkl.zst"):
            if old != dest:
                old.unlink(missing_ok=True)
    except OSError:
        pass
    return ticks


def cached_fact(path, name, compute, version=1, raw=False):
    """compute(path) -> JSON-able value, cached per recording and fact name.
    raw=True: compute reads the JSON itself (not via raw_ticks), so it cannot
    run on a stub."""
    stem = Path(path).name
    dest = META / f"{stem}.{name}.{_key(path, f'{name}v{version}')}.json"
    if dest.exists():
        try:
            return json.loads(dest.read_text(encoding="utf-8"))["v"]
        except Exception:
            pass
    if is_stub(path) and raw:
        raise StubMiss(f"{rec_id(path)}: fact '{name}' reads the JSON and was not cached - sync again")
    v = compute(path)
    try:
        _write_atomic(dest, json.dumps({"v": v}).encode("utf-8"))
        for old in META.glob(f"{stem}.{name}.*.json"):
            if old != dest:
                old.unlink(missing_ok=True)
    except OSError:
        pass
    return v


def cached_pickle(path, name, compute, version=1):
    """Like cached_fact, for results that are not JSON-shaped (tuples, objects)."""
    stem = Path(path).name
    dest = META / f"{stem}.{name}.{_key(path, f'{name}v{version}')}.pkl.zst"
    if dest.exists():
        try:
            return pickle.loads(_z.decompress(dest.read_bytes()))
        except Exception:
            dest.unlink(missing_ok=True)
    v = compute(path)
    try:
        _write_atomic(dest, _z.compress(pickle.dumps(v, protocol=5)))
        for old in META.glob(f"{stem}.{name}.*.pkl.zst"):
            if old != dest:
                old.unlink(missing_ok=True)
    except OSError:
        pass
    return v


def _digest_path(path):
    return TICKS / f"{Path(path).name}.{_key(path, f'v{TICK_VERSION}')}.pkl.zst"


def _digest_one(path):
    # A stub's digest is synced, never built; an existing one is not re-read.
    if not is_stub(path) and not _digest_path(path).exists():
        raw_ticks(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--migrate", action="store_true", help="rename path-keyed cache files to rec_id keys")
    a = ap.parse_args()
    files = sorted(glob.glob("runs/*.jsonl")) + sorted(glob.glob("botruns/*.jsonl"))
    # Still being recorded: its digest would be stale (and keyed to a size
    # it will not keep) by the time it is used.
    now = time.time()
    files = [f for f in files if os.path.getsize(f) == 0 or now - os.path.getmtime(f) > LIVE_S]
    if a.migrate:
        print(f"renamed {migrate_keys(files)} cache files")
        return
    if a.stats:
        src = sum(src_sizes(files))
        dig = sum(p.stat().st_size for p in TICKS.glob("*.pkl.zst"))
        print(f"{len(files)} recordings, {src / 1e9:.2f} GB JSON; "
              f"{len(list(TICKS.glob('*.pkl.zst')))} digests, {dig / 1e6:.0f} MB")
        return
    from concurrent.futures import ProcessPoolExecutor
    from .parallel import safe_worker_count
    sizes = src_sizes(files)
    with ProcessPoolExecutor(max_workers=safe_worker_count(sizes, os.cpu_count() or 4)) as ex:
        for i, _ in enumerate(ex.map(_digest_one, files), 1):
            if i % 50 == 0:
                print(f"  digested {i}/{len(files)}", flush=True)
    print(f"done: {len(files)} recordings")


if __name__ == "__main__":
    main()
