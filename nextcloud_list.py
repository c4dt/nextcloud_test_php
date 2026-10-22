#!/usr/bin/env python3
r"""
Show build ("created") times of the supported Nextcloud tags listed on
https://hub.docker.com/_/nextcloud/, plus the PHP
base image each one was built FROM, without pulling any images.

The official images carry OCI annotations in their image index
(org.opencontainers.image.base.name / .base.digest), so we can tell exactly
which php image a Nextcloud image was built on, when that php image was built,
and whether it is still the current one for that php tag.

Docker Hub rate-limits manifest requests (anonymous: ~100/hour per IP).
Results are cached in .nextcloud_cache.json (digests are immutable), so when
the limit is hit just rerun later and it continues where it stopped.
Set DOCKERHUB_USERNAME and DOCKERHUB_TOKEN to use your account's higher limit.

Usage:
  devbox run list [args]                       # or: python3 nextcloud_list.py
  ./nextcloud_list.py                          # all supported tags
  ./nextcloud_list.py --tag-regex '^\d+\.\d+\.\d+(-apache|-fpm)?$'
  ./nextcloud_list.py --base-after 2026-10-21T15:00:00Z
  ./nextcloud_list.py --platform linux/arm64/v8 --json
"""
import argparse
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

REPO = "library/nextcloud"
BASE_REPO = "library/php"
HUB_REPO = f"https://hub.docker.com/v2/repositories/{REPO}/"
HUB_TAGS = f"{HUB_REPO}tags"
REGISTRY = "https://registry-1.docker.io/v2"
AUTH = "https://auth.docker.io/token"
ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".nextcloud_cache.json")

session = requests.Session()
_token = {"value": None}
_lock = threading.Lock()
rate_limited = threading.Event()


class RateLimited(Exception):
    pass


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


cache = load_cache()


def refresh_token():
    user, pw = os.environ.get("DOCKERHUB_USERNAME"), os.environ.get("DOCKERHUB_TOKEN")
    r = session.get(AUTH, params={
        "service": "registry.docker.io",
        "scope": [f"repository:{REPO}:pull", f"repository:{BASE_REPO}:pull"],
    }, auth=(user, pw) if user and pw else None, timeout=30)
    r.raise_for_status()
    _token["value"] = r.json()["token"]


def registry_get(repo, path, accept=None):
    """GET from the registry, refreshing the token once on 401."""
    for attempt in (1, 2):
        if rate_limited.is_set():
            raise RateLimited()
        with _lock:
            if not _token["value"]:
                refresh_token()
            token = _token["value"]
        headers = {"Authorization": f"Bearer {token}"}
        if accept:
            headers["Accept"] = accept
        r = session.get(f"{REGISTRY}/{repo}{path}", headers=headers, timeout=30)
        if r.status_code == 401 and attempt == 1:
            with _lock:
                if _token["value"] == token:
                    _token["value"] = None
            continue
        if r.status_code == 429:
            rate_limited.set()
            raise RateLimited()
        r.raise_for_status()
        return r.json()


def cached(key, fn):
    """Cache results of immutable (digest-addressed) lookups."""
    if key in cache:
        return cache[key]
    value = fn()
    cache[key] = value
    return value


def list_tags(tag_regex):
    """Tags from the "Supported tags" section of https://hub.docker.com/_/nextcloud/."""
    r = session.get(HUB_REPO, timeout=30)
    r.raise_for_status()
    desc = r.json()["full_description"]
    section = desc.split("Supported tags", 1)[1].split("\n#", 1)[0]
    names = []
    for line in section.splitlines():
        if line.lstrip().startswith("-"):         # "-\t[`33.0.10-apache`, `33.0`, ...](...)"
            names += re.findall(r"`([^`]+)`", line.split("](", 1)[0])
    if tag_regex:
        pattern = re.compile(tag_regex)
        names = [n for n in names if pattern.search(n)]

    def hub_tag(name):
        r = session.get(f"{HUB_TAGS}/{name}", timeout=30)
        r.raise_for_status()
        t = r.json()
        return {"tag": name, "digest": t.get("digest"),
                "hub_last_updated": t.get("last_updated")}
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(hub_tag, names))


def pick_platform(index, os_name, arch, variant):
    for m in index.get("manifests", []):
        p = m.get("platform", {})
        if p.get("os") == os_name and p.get("architecture") == arch \
                and (not variant or p.get("variant") == variant):
            return m
    return None


def config_created(repo, manifest_digest):
    """Platform manifest -> config blob -> 'created' (blob GETs aren't rate-limited)."""
    def fetch():
        manifest = registry_get(repo, f"/manifests/{manifest_digest}", accept=ACCEPT)
        config = registry_get(repo, f"/blobs/{manifest['config']['digest']}")
        return config.get("created")
    return cached(f"created:{repo}@{manifest_digest}", fetch)


def image_info(digest, os_name, arch, variant):
    """Index digest -> created time and php base image for one platform."""
    index = cached(f"index:{digest}",
                   lambda: registry_get(REPO, f"/manifests/{digest}", accept=ACCEPT))
    if "manifests" not in index:                  # old single-platform image
        created = cached(f"created:{REPO}@{digest}",
                         lambda: registry_get(REPO, f"/blobs/{index['config']['digest']}")
                         .get("created"))
        return {"created": created}
    m = pick_platform(index, os_name, arch, variant)
    if not m:
        return {"error": "platform not found"}
    ann = m.get("annotations", {})
    created = ann.get("org.opencontainers.image.created") \
        or config_created(REPO, m["digest"])
    return {
        "created": created,
        "base_name": ann.get("org.opencontainers.image.base.name"),
        "base_digest": ann.get("org.opencontainers.image.base.digest"),
    }


def current_base_digest(base_name, os_name, arch, variant):
    """Digest that a php tag points to *now* (not cached: tags move)."""
    tag = base_name.split(":", 1)[1]
    index = registry_get(BASE_REPO, f"/manifests/{tag}", accept=ACCEPT)
    m = pick_platform(index, os_name, arch, variant)
    return m["digest"] if m else None


def run_parallel(fn, items, workers):
    """Map fn over items; per-item errors become {'error': ...}."""
    def work(item):
        try:
            return item, fn(item)
        except RateLimited:
            return item, {"error": "rate-limited, rerun later"}
        except Exception as e:                    # keep going on per-image errors
            return item, {"error": str(e)}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(work, items))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag-regex", help="only include tags matching this regex")
    ap.add_argument("--platform", default="linux/amd64",
                    help="os/arch[/variant] (default linux/amd64)")
    ap.add_argument("--base-after", metavar="TIMESTAMP",
                    help="mark images whose php base was built before this "
                         "ISO-8601 UTC time (e.g. 2026-10-21T15:00:00Z)")
    ap.add_argument("--json", action="store_true", help="output JSON instead of a table")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    parts = args.platform.split("/")
    if len(parts) < 2:
        ap.error("--platform must be os/arch[/variant], e.g. linux/amd64")
    plat = (parts[0], parts[1], parts[2] if len(parts) > 2 else None)

    tags = list_tags(args.tag_regex)
    if not tags:
        sys.exit("No tags found.")

    try:
        # Many tags (33, 33.0, 33.0.10 ...) share one digest: resolve each digest once.
        digests = sorted({t["digest"] for t in tags if t["digest"]})
        print(f"{len(tags)} tags, {len(digests)} distinct images", file=sys.stderr)
        infos = run_parallel(lambda d: image_info(d, *plat), digests, args.workers)

        base_names = sorted({i["base_name"] for i in infos.values() if i.get("base_name")})
        current = run_parallel(lambda n: current_base_digest(n, *plat), base_names, args.workers)

        base_digests = sorted({i["base_digest"] for i in infos.values() if i.get("base_digest")})
        base_built = run_parallel(lambda d: config_created(BASE_REPO, d), base_digests,
                                  args.workers)
    finally:
        save_cache(cache)

    rows = []
    for t in tags:
        info = infos.get(t["digest"], {"error": "no digest on Hub"})
        bd = info.get("base_digest")
        built = base_built.get(bd) if bd else None
        cur = current.get(info.get("base_name"))
        row = {**t, "created": info.get("created"), "error": info.get("error"),
               "base_name": info.get("base_name"), "base_digest": bd,
               "base_built": built if isinstance(built, str) else None,
               "base_is_current": (cur == bd) if isinstance(cur, str) and bd else None}
        if args.base_after:
            row["base_ok"] = row["base_built"] >= args.base_after if row["base_built"] else None
        rows.append(row)

    rows.sort(key=lambda r: r["created"] or "", reverse=True)

    if rate_limited.is_set():
        print("WARNING: hit Docker Hub rate limit; some rows are incomplete. "
              "Rerun later (results are cached) or set DOCKERHUB_USERNAME/DOCKERHUB_TOKEN.",
              file=sys.stderr)

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    def fmt(v):
        return "-" if v is None else str(v)

    w = max(len(r["tag"]) for r in rows)
    bw = max([len(r["base_name"] or "-") for r in rows] + [4])
    header = f"{'TAG':<{w}}  {'CREATED':<30}  {'BASE':<{bw}}  {'BASE BUILT':<30}  CURRENT"
    if args.base_after:
        header += "  AFTER"
    print(header)
    for r in rows:
        created = r["created"] or f"n/a ({r['error']})"
        cur = {True: "yes", False: "no", None: "-"}[r["base_is_current"]]
        line = (f"{r['tag']:<{w}}  {created:<30}  {fmt(r['base_name']):<{bw}}  "
                f"{fmt(r['base_built']):<30}  {cur:<7}")
        if args.base_after:
            line += "  " + {True: "yes", False: "NO", None: "-"}[r["base_ok"]]
        print(line.rstrip())


if __name__ == "__main__":
    main()
