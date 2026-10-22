# nextcloud_list

Lists the supported tags of the official [Nextcloud Docker image](https://hub.docker.com/_/nextcloud/)
with their build time and the PHP base image each one was built from, **without pulling any images**.

It reads the OCI annotations (`org.opencontainers.image.base.name` / `.base.digest`)
from each image index, so for every tag you see:

- when the Nextcloud image was built,
- which `php:` image it was built `FROM` and when that was built,
- whether that base is still the current image for its `php` tag,
- optionally, whether the base was built after a given time (e.g. after a PHP security fix).

## Setup

Uses [devbox](https://www.jetify.com/devbox) (Python 3.12 + `requests`):

```sh
devbox run list            # or, inside `devbox shell`: python3 nextcloud_list.py
```

## Usage

```sh
./nextcloud_list.py                                          # all supported tags
./nextcloud_list.py --tag-regex '^\d+\.\d+\.\d+(-apache|-fpm)?$'
./nextcloud_list.py --base-after 2026-10-21T15:00:00Z        # adds an AFTER column
./nextcloud_list.py --platform linux/arm64/v8 --json
```

| Option | Description |
| --- | --- |
| `--tag-regex REGEX` | only include tags matching this regex |
| `--platform os/arch[/variant]` | platform to inspect (default `linux/amd64`) |
| `--base-after TIMESTAMP` | flag images whose PHP base was built before this ISO-8601 UTC time |
| `--json` | output JSON instead of a table |
| `--workers N` | parallel requests (default 8) |

## Rate limits and caching

Docker Hub limits anonymous manifest requests (~100/hour per IP). Results for
immutable digests are cached in `.nextcloud_cache.json`, so if you hit the limit
just rerun later and it continues where it stopped. For a higher limit, set:

```sh
export DOCKERHUB_USERNAME=...
export DOCKERHUB_TOKEN=...
```
