"""Publish the model in models/ as a GitHub release, for everyone's GUI.

    training\\release_model.bat                     # asks before publishing
    training\\release_model.bat --notes "beats gen 258 7-3"
    python training/release_model.py --dry-run      # only build the zip

Packs models/ (ajbot/modelpack.py) into dist/ajbot-model-<tag>.zip and
publishes it as release <tag> of the public repo, not marked "latest" (the
GUI's own builds keep that). Users press Get latest model on the Train page
(tools/get_model.py) to install it. Only the newest --keep model releases
are kept.

Login: GH_TOKEN / GITHUB_TOKEN if set, else whatever Git's credential manager
holds for github.com (the login `git push` uses). The token needs to be able
to write releases (the "repo" scope, or Contents: write).
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ajbot import modelpack

REPO = "DirectalArrowYT/ajbot"
API = f"https://api.github.com/repos/{REPO}"


def token():
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    p = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True)
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("no GitHub login: set GH_TOKEN, or push to GitHub once with git "
                     "so its credential manager has one")


def call(method, url, tok, body=None, data=None, ctype=None, size=None):
    headers = {"Authorization": f"Bearer {tok}", "User-Agent": "ajbot-release",
               "Accept": "application/vnd.github+json"}
    if body is not None:
        data = json.dumps(body).encode()
        ctype = "application/json"
    if ctype:
        headers["Content-Type"] = ctype
    if size is not None:
        headers["Content-Length"] = str(size)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise SystemExit(f"GitHub said {e.code} to {method} {url}:\n{e.read().decode(errors='replace')[:400]}")


def last_comparison():
    """The latest 'N matches, then compare' verdict for this policy, if any."""
    p = Path("stats/compare.jsonl")
    if not p.exists():
        return ""
    try:
        e = json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ""
    if Path(e.get("a", "")).name != "policy.pkl":
        return ""
    return f"Last local comparison ({e.get('matches')} matches vs {Path(e['b']).stem}): {e.get('verdict')}"


def prune(tok, keep):
    rels = call("GET", f"{API}/releases?per_page=100", tok) or []
    models = [r for r in rels if r.get("tag_name", "").startswith("model-")
              and any(a["name"].startswith(modelpack.PREFIX) for a in r.get("assets", []))]
    for r in models[keep:]:
        print(f"removing old model release {r['tag_name']}")
        call("DELETE", f"{API}/releases/{r['id']}", tok)
        call("DELETE", f"{API}/git/refs/tags/{urllib.parse.quote(r['tag_name'])}", tok)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--notes", default="", help="a line about this model for the release page")
    ap.add_argument("--keep", type=int, default=5, help="model releases to keep (default 5)")
    ap.add_argument("--yes", action="store_true", help="do not ask before publishing")
    ap.add_argument("--dry-run", action="store_true", help="build the zip, publish nothing")
    a = ap.parse_args()

    tag = time.strftime("model-%Y.%m.%d-%H%M")
    print(f"packing models/ as {tag} ...")
    zpath, man = modelpack.pack(Path("dist"), tag, a.notes)
    mb = zpath.stat().st_size / 1e6
    print(f"  {zpath} ({mb:.0f} MB, {len(man['files'])} files)")
    for f, m in man["files"].items():
        print(f"    {f:<22} {m['size'] / 1e6:8.1f} MB  {m['sha256'][:12]}")
    gen = man.get("league_gen")
    lines = [a.notes] if a.notes else []
    if gen is not None:
        lines.append(f"League champion gen {gen}.")
    cmp_line = last_comparison()
    if cmp_line:
        lines.append(cmp_line)
    lines += ["", "Install: GUI > Train > **Get latest model** (or `python tools/get_model.py`).",
              "Your current model is kept in models/replaced-<time>/ and can be picked as Bot B.",
              "", "Files: " + ", ".join(man["files"])]
    body = "\n".join(lines)
    if a.dry_run:
        print("\n--- release notes ---\n" + body + "\n--- dry run: nothing published ---")
        return
    if not a.yes:
        ans = input(f"\nPublish {zpath.name} publicly on github.com/{REPO}? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("not published (the zip stays in dist/)")
            return
    tok = token()
    rel = call("POST", f"{API}/releases", tok, body={
        "tag_name": tag, "target_commitish": "main",
        "name": f"Model {tag[6:]}" + (f" (gen {gen})" if gen is not None else ""),
        "body": body, "make_latest": "false"})
    print(f"release {tag} created, uploading {mb:.0f} MB ...")
    up = (f"https://uploads.github.com/repos/{REPO}/releases/{rel['id']}/assets"
          f"?name={urllib.parse.quote(zpath.name)}")
    with open(zpath, "rb") as f:
        call("POST", up, tok, data=f, ctype="application/zip", size=zpath.stat().st_size)
    print(f"published: {rel['html_url']}")
    prune(tok, a.keep)


if __name__ == "__main__":
    main()
