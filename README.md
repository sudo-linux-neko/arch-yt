# The Plain Bagel Preservation Archiver

Resumable, Kaggle-friendly preservation pipeline for public YouTube videos from
[The Plain Bagel](https://www.youtube.com/@ThePlainBagel), using `yt-dlp`, FFmpeg,
and the Internet Archive.

The archive is **non-commercial**, created with the creator's written permission.
The discovery snapshot (`work/state/discovery.json`) also preserves the channel
title, description, ID, URL, and follower count.
All copyright remains with Richard Coffin / The Plain Bagel. The material is
**NOT public domain**, has **no Creative Commons license**, and carries **no
commercial license**. The complete permission correspondence is held privately;
only a permission summary is published.

## Important archive.org policy warning

Internet Archive generally discourages mirroring material that is still online
unless there is a preservation need, and it may remove items or restrict
accounts that do so. Before bulk publication, contact `info@archive.org` and/or
the creator and get the plan cleared. This software cannot grant that clearance.

The target collection does not exist yet. Items currently go to Community Video
(`opensource_movies`) while a dedicated collection is requested. A dedicated
archive.org collection normally requires a list of 50+ related items and an
email request to `info@archive.org`. Use `collection-request` after archiving
enough items, then `migrate-collection` after the collection is created.

## What it archives per video

One archive.org item per video:

```text
theplainbagel-<YouTube video ID>
```

The original case of the YouTube ID is preserved. Each item contains:

- `video-720p-h264.mp4`
- `video-720p-av1.mp4` (or VP9/VP8 WebM fallback at the same resolution)
- `video-480p-h264.mp4`
- `video-480p-av1.mp4` (or VP9/VP8 WebM fallback at the same resolution)
- `master-<height>p-<codec>.mp4` or `.webm` — best available source stream
- `DESCRIPTION.txt` — the complete original YouTube description, byte-for-byte
- `RIGHTS-AND-ATTRIBUTION.txt`
- `metadata.json`
- `source.info.json` — complete `yt-dlp` metadata at capture time
- `thumbnail.jpg`
- English captions when available
- `chapters.txt` when available
- `archive-manifest.json`
- `SHA256SUMS`

Video streams are remuxed without video re-encoding. If a requested resolution
or codec is unavailable, the closest lower resolution or the documented codec
fallback is used and the item metadata records the fallback. Audio is copied when
the container allows it and transcoded only when necessary (for example AAC to
Opus for WebM).

## Requirements

- Linux with Python 3.10 or newer
- `ffmpeg` and `ffprobe` on `PATH`
- Internet Archive account and S3 keys from
  <https://archive.org/account/s3.php>
- Disk space; a single 4K item can require several GB temporarily. Default reserve
  is 12 GiB.

The local tested environment is `~/ml_env`:

```bash
source ~/ml_env/bin/activate
pip install -r requirements.txt
pip install -e .
```

For Internet Archive credentials:

```bash
export IA_ACCESS_KEY="..."
export IA_SECRET_KEY="..."
```

Optional YouTube cookies, if YouTube requires them:

```bash
export YT_COOKIES_FILE="/path/to/cookies.txt"
```

## Commands

Run all commands through the active Python environment. The default config is
`config.toml`; `--config` and `--work-dir` are global options.

```bash
# Check installed tools, config, archive.org access, and disk space.
python -m plainbagel_archiver doctor

# Discover videos, Shorts, and past livestream VODs.
python -m plainbagel_archiver discover

# Archive up to 10 pending videos in this session.
python -m plainbagel_archiver run --limit 10

# Test one specific video first. This performs real downloads and uploads.
python -m plainbagel_archiver run --limit 1 --only-id OjyPy_4eVdw

# Pause after the current video finishes; resume by clearing the marker.
python -m plainbagel_archiver pause
python -m plainbagel_archiver resume

# Show local state and completed item links.
python -m plainbagel_archiver status

# Retry failed/interrupted items.
python -m plainbagel_archiver reset-failed

# Verify published items against their remote manifest and checksums.
python -m plainbagel_archiver verify

# Check archive.org even for records still marked pending locally.
python -m plainbagel_archiver run --reconcile --limit 10

# Generate the collection request text after 50+ items are complete.
python -m plainbagel_archiver collection-request

# After the dedicated collection exists, add complete items to it.
python -m plainbagel_archiver migrate-collection --collection theplainbagel-archive
```

`run` protects against two concurrent processes with a lock file. On startup it
resets interrupted in-progress records, checks archive.org first when a previous
attempt exists, and uses remote `archive_status=complete` as the source of truth.
Remote uploads use checksum-based skipping, so a stopped session can be retried.
Local media is removed after successful upload and verification unless
`cleanup_after_upload = false` is set.

Active livestreams and upcoming premieres are recorded and deferred, then retried
on a later run. Private, members-only, and otherwise unavailable videos are not
downloaded. The tool does not bypass DRM, paywalls, or private access.

## Configuration

See `config.toml`. Defaults:

```toml
collection = "opensource_movies"
identifier_prefix = "theplainbagel"
work_dir = "./work"
permission_acknowledged = true
cleanup_after_upload = true
session_minutes = 600
min_free_gb = 12.0
tabs = ["videos", "shorts", "streams"]
subtitle_languages = "en.*"
```

Changing `collection` affects future uploads. Existing items are not moved
automatically; use `migrate-collection` after creating a dedicated collection.

## Kaggle

Use Kaggle Secrets for `IA_ACCESS_KEY` and `IA_SECRET_KEY`; never hard-code them.
The example notebook is `kaggle/plainbagel_archive.ipynb`. Keep the `work`
directory on persistent Kaggle storage if you need local resume state, and save
the notebook output or attach it as a dataset before the session ends. Even
without local state, archive.org checksum skipping and `--reconcile` can recover
completed items. Kaggle sessions are time-limited, so start with `--limit` and
short runs.

## Collection request and migration

`collection-request` generates an email draft and item list. It uses only local
complete records, so run `verify` and make sure item links are live before sending.
The request should go to `info@archive.org`; the current suggested dedicated
collection identifier is `theplainbagel-archive`.

`migrate-collection` adds existing items to a new collection while keeping their
current collection membership. Use `--replace` only after confirming that removing
the old membership is intended. Update `collection` in `config.toml` afterward for
new uploads.

## Tests

```bash
pytest
ruff check .
```

The test suite covers format selection and fallbacks, vertical Shorts, metadata
and rights preservation, SQLite resume state, FFmpeg muxing and audio fallback,
bundle/checksum construction, archive collision handling, collection requests,
and locks. Real download/mux smoke tests were performed manually with the vertical
Short `OjyPy_4eVdw` and the landscape video `J_389UPEf68`; live archive.org uploads
require the user's S3 credentials and have not been performed in this workspace.

## Documentation

- `docs/archive-org-setup.md` — account, policy clearance, collection request, migration
- `docs/kaggle.md` — Kaggle internet, secrets, persistence, and disk notes

## Contacts

- Creator / rights holder: Richard Coffin `<richard@theplainbagel.com>`
- Archiver: `<nekomeowpurr@proton.me>`
- Internet Archive collection requests: `info@archive.org`
