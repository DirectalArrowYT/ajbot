"""A trained model as one zip, for GitHub releases.

training/release_model.bat packs models/ into ajbot-model-<tag>.zip and
publishes it as a release of the public repo. tools/get_model.py (the GUI's
Get latest model button) downloads the newest one and installs it.

What goes in is what the bot reads at play time: the policy, its per-model
data, and the tables built from the game's own files (frame data, move
data, the CPU's combo lists), which a user without that export cannot build.
What stays out is personal: saved combos, drills, the tag book.

Every file is scrubbed and then scanned before it goes in (scrub/leaks):
the route tables name the recordings they were mined from, and a release
is public. Packing refuses while anything private is left.
"""
import getpass
import hashlib
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

PREFIX = "ajbot-model"
MODELS = Path("models")
STAMP = MODELS / "model_release.json"       # which release is installed
MANIFEST = "manifest.json"
REQUIRED = ["policy.pkl"]
FILES = REQUIRED + [
    # per model (what the league champion carries)
    "combobook.json", "moveinfo.json", "combos.json", "learned_actions.json",
    "threat.json", "defense.json", "escape.json", "mined_routes.json",
    "drilled_routes.json",
    # from the game's files
    "framedata.json", "movedata.json", "movewindows.json", "ai_routes.json", "authored_routes.json",
    "notation_gaps.json",
]
KEEP_REPLACED = 3


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------ privacy
_REC = re.compile(r"\b(?:bot)?runs[/\\][\w.\-]+\.jsonl")
_SRC_PRIVATE = ("file", "t0", "t1")     # mined route "src": which recording, when


def _scrub_obj(o):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if k == "src" and isinstance(v, dict):
                v = {kk: vv for kk, vv in v.items() if kk not in _SRC_PRIVATE}
            out[k] = _scrub_obj(v)
        return out
    if isinstance(o, list):
        return [_scrub_obj(v) for v in o]
    if isinstance(o, str):
        return _REC.sub("a recording", o)
    return o


def scrub(name, data):
    """The bytes to publish for one model file: JSON with recording names
    and times taken out; anything else unchanged."""
    if not name.endswith(".json"):
        return data
    obj = json.loads(data.decode("utf-8"))
    return json.dumps(_scrub_obj(obj), separators=(",", ":")).encode("utf-8")


def _private_patterns():
    pats = [rb"\b(?:\d{1,3}\.){3}\d{1,3}\b(?!\.\d)",          # IPv4
            rb"\b[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9-]{2,}\.(?:com|net|org|io|dev|edu|me|co)\b",  # email
            rb"/(?:mnt|home|Users|root)/",                       # POSIX paths
            rb"\b[A-Za-z]:[\\/](?:Users|Documents)",            # Windows paths
            rb"steamapps", _REC.pattern.encode()]
    for v in {getpass.getuser(), Path.home().name}:
        if v and len(v) >= 4:
            pats.append(re.escape(v.encode()))
    return re.compile(b"|".join(pats), re.I)


def leaks(name, data, limit=5):
    """Private-looking strings left in one file's bytes (pickles hold their
    strings as raw UTF-8, so one byte scan covers JSON and policy.pkl)."""
    found = []
    for m in _private_patterns().finditer(data):
        hit = m.group(0)
        if re.fullmatch(rb"(?:\d{1,3}\.){3}\d{1,3}", hit) and not _ipv4(hit):
            continue
        found.append(hit.decode("utf-8", "replace"))
        if len(found) >= limit:
            break
    return found


def _ipv4(b):
    """A real dotted address, not a version or a float list."""
    parts = [int(x) for x in b.split(b".")]
    return all(p <= 255 for p in parts) and parts[0] not in (0,) and not all(p < 10 for p in parts)


def pack(out_dir, tag, notes="", models=MODELS):
    """Zip the model in `models` to out_dir/ajbot-model-<tag>.zip."""
    missing = [f for f in REQUIRED if not (models / f).exists()]
    if missing:
        raise SystemExit(f"nothing to release: {', '.join(missing)} missing in {models}")
    files = [f for f in FILES if (models / f).exists()]
    data, bad = {}, []
    for f in files:
        data[f] = scrub(f, (models / f).read_bytes())
        hits = leaks(f, data[f]) + leaks("notes", notes.encode("utf-8"))
        if hits:
            bad.append(f"{f}: {', '.join(sorted(set(hits)))}")
    if bad:
        raise SystemExit("private data left in the model - not packing:\n  " + "\n  ".join(bad))
    manifest = {"tag": tag, "created": round(time.time()), "notes": notes,
                "files": {f: {"sha256": hashlib.sha256(data[f]).hexdigest(),
                              "size": len(data[f])} for f in files}}
    champ = models / "champion.json"
    if champ.exists():
        try:
            manifest["league_gen"] = json.loads(champ.read_text()).get("gen")
        except ValueError:
            pass
    out_dir.mkdir(parents=True, exist_ok=True)
    zpath = out_dir / f"{PREFIX}-{tag.removeprefix('model-')}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in files:
            z.writestr(f, data[f])
        z.writestr(MANIFEST, json.dumps(manifest, indent=1))
    return zpath, manifest


def installed():
    try:
        return json.loads(STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def install(zpath, tag, models=MODELS, say=print):
    """Check every file against the manifest, move the current model to
    models/replaced-<time>/ (pickable as Bot B), then put the new one in."""
    with zipfile.ZipFile(zpath) as z:
        manifest = json.loads(z.read(MANIFEST))
        stage = models / f".incoming-{int(time.time())}"
        stage.mkdir(parents=True)
        try:
            for f, meta in manifest["files"].items():
                if "/" in f or "\\" in f or f.startswith("."):
                    raise SystemExit(f"refusing odd file name in the zip: {f!r}")
                z.extract(f, stage)
                if sha256(stage / f) != meta["sha256"]:
                    raise SystemExit(f"{f}: checksum mismatch - download again")
            if (models / "policy.pkl").exists():
                keep = models / time.strftime("replaced-%Y%m%d-%H%M%S")
                keep.mkdir()
                for f in manifest["files"]:
                    if (models / f).exists():
                        shutil.copy2(models / f, keep / f)
                say(f"current model kept in {keep} (pick it as Bot B to compare)")
                old = sorted(models.glob("replaced-*"))
                for d in old[:-KEEP_REPLACED]:
                    shutil.rmtree(d, ignore_errors=True)
            for f in manifest["files"]:
                (stage / f).replace(models / f)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    STAMP.write_text(json.dumps({"tag": tag, "installed": round(time.time()),
                                 "league_gen": manifest.get("league_gen"),
                                 "files": sorted(manifest["files"])}, indent=1))
    say(f"installed model {tag} ({len(manifest['files'])} files)")
