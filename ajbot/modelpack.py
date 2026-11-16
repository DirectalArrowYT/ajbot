"""A trained model as one zip, for GitHub releases.

training/release_model.bat packs models/ into ajbot-model-<tag>.zip and
publishes it as a release of the public repo. tools/get_model.py (the GUI's
Get latest model button) downloads the newest one and installs it.

What goes in is what the bot reads at play time: the policy, its per-model
data, and the tables built from the game's own files (frame data, move
data, the CPU's combo lists), which a user without that export cannot build.
What stays out is personal: saved combos, drills, the tag book.
"""
import hashlib
import json
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
    "framedata.json", "movedata.json", "ai_routes.json", "authored_routes.json",
    "notation_gaps.json",
]
KEEP_REPLACED = 3


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pack(out_dir, tag, notes="", models=MODELS):
    """Zip the model in `models` to out_dir/ajbot-model-<tag>.zip."""
    missing = [f for f in REQUIRED if not (models / f).exists()]
    if missing:
        raise SystemExit(f"nothing to release: {', '.join(missing)} missing in {models}")
    files = [f for f in FILES if (models / f).exists()]
    manifest = {"tag": tag, "created": round(time.time()), "notes": notes,
                "files": {f: {"sha256": sha256(models / f),
                              "size": (models / f).stat().st_size} for f in files}}
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
            z.write(models / f, f)
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
