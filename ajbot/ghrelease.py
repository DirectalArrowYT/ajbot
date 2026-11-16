"""Publishing GitHub releases of the public repo (models, game files).

Login: GH_TOKEN / GITHUB_TOKEN if set, else whatever Git's credential manager
holds for github.com (the login `git push` uses). The token needs to be able
to write releases (the "repo" scope, or Contents: write).
"""
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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


def confirm(what):
    ans = input(f"\nPublish {what} publicly on github.com/{REPO}? [y/N] ")
    return ans.strip().lower() in ("y", "yes")


def publish(tok, tag, name, body, asset: Path):
    """New release `tag` (never marked latest: the GUI builds keep that) with
    one zip attached. Returns the release's web URL."""
    rel = call("POST", f"{API}/releases", tok, body={
        "tag_name": tag, "target_commitish": "main", "name": name,
        "body": body, "make_latest": "false"})
    mb = asset.stat().st_size / 1e6
    print(f"release {tag} created, uploading {asset.name} ({mb:.1f} MB) ...")
    up = (f"https://uploads.github.com/repos/{REPO}/releases/{rel['id']}/assets"
          f"?name={urllib.parse.quote(asset.name)}")
    with open(asset, "rb") as f:
        call("POST", up, tok, data=f, ctype="application/zip", size=asset.stat().st_size)
    return rel["html_url"]


def prune(tok, tag_prefix, asset_prefix, keep, what="release"):
    """Delete all but the newest `keep` releases tagged tag_prefix* that carry
    an asset_prefix* file (release and tag)."""
    rels = call("GET", f"{API}/releases?per_page=100", tok) or []
    mine = [r for r in rels if r.get("tag_name", "").startswith(tag_prefix)
            and any(a["name"].startswith(asset_prefix) for a in r.get("assets", []))]
    for r in mine[keep:]:
        print(f"removing old {what} {r['tag_name']}")
        call("DELETE", f"{API}/releases/{r['id']}", tok)
        call("DELETE", f"{API}/git/refs/tags/{urllib.parse.quote(r['tag_name'])}", tok)
