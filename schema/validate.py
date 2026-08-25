#!/usr/bin/env python3
"""
Catalog validator for naamakel-web.

This is the most important safeguard in the content pipeline. Installed apps
fetch these files directly, so a malformed or inconsistent commit reaches every
user within hours. The app keeps its last-known-good copy when a fetch fails
validation on-device, but that is the second line of defence. This is the
first, and it runs before merge.

Exit code 1 on any ERROR. WARNINGs are advisory and do not fail the build.
"""
import json
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
SCHEMA_VERSION = 1

ART_ID = re.compile(r"^ar_[a-z0-9_]+$")
SONG_ID = re.compile(r"^sg_\d{6}$")
ASCII_PATH = re.compile(r"^[A-Za-z0-9._/\-]+$")

errors = []
warnings = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def load(name):
    p = DATA / name
    if not p.exists():
        err(name + ": missing")
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        err("{}: invalid JSON at line {} col {}: {}".format(name, e.lineno, e.colno, e.msg))
        return None


def nonempty(d, key, where):
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        err("{}: {} must be a non-empty string".format(where, key))
        return False
    return True


def check_manifest(m):
    if m.get("schemaVersion") != SCHEMA_VERSION:
        err("manifest: schemaVersion must be {}, got {!r}".format(SCHEMA_VERSION, m.get("schemaVersion")))
    if not isinstance(m.get("dataVersion"), int):
        err("manifest: dataVersion must be an integer (bump it on every content change)")

    url_keys = ("artistsUrl", "songsUrl", "audioBaseUrl", "imageBaseUrl",
                "songRequestUrl", "privacyPolicyUrl", "aboutUrl")
    for key in url_keys:
        v = m.get(key)
        if not isinstance(v, str) or not v.startswith("https://"):
            err("manifest: {} must be an absolute https URL, got {!r}".format(key, v))

    for key in ("audioBaseUrl", "imageBaseUrl"):
        v = m.get(key)
        if isinstance(v, str) and not v.endswith("/"):
            err("manifest: {} must end with a slash (paths are appended verbatim)".format(key))

    # The deep-link host has a DOUBLE 'a'. Getting it wrong breaks App Links
    # silently, so assert it rather than trusting review.
    for key in ("artistsUrl", "songsUrl", "privacyPolicyUrl", "aboutUrl"):
        v = m.get(key, "")
        if isinstance(v, str) and "aanmeegasaaral.in" in v and "naamaakel." not in v:
            err("manifest: {} should use host naamaakel.aanmeegasaaral.in (double a), got {!r}".format(key, v))

    if "REPLACE_ME" in str(m.get("contactEmail", "")):
        warn("manifest: contactEmail is still the placeholder")

    if not isinstance(m.get("minSupportedVersionCode"), int):
        err("manifest: minSupportedVersionCode must be an integer")


def check_artists(doc):
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        err("artists: schemaVersion must be {}".format(SCHEMA_VERSION))
    items = doc.get("artists")
    if not isinstance(items, list) or not items:
        err("artists: artists must be a non-empty array")
        return {}

    seen = {}
    for i, art in enumerate(items):
        where = "artists[{}]".format(i)
        aid = art.get("id")
        if not isinstance(aid, str) or not ART_ID.match(aid):
            err("{}: id must match ar_[a-z0-9_]+, got {!r}".format(where, aid))
            continue
        if aid in seen:
            err("{}: duplicate artist id {!r}".format(where, aid))
        seen[aid] = art

        nonempty(art, "nameTa", where)
        nonempty(art, "nameEn", where)

        photo = art.get("photoPath")
        if photo is not None and (not isinstance(photo, str) or not ASCII_PATH.match(photo)):
            err("{}: photoPath must be an ASCII path, got {!r}".format(where, photo))
        if not isinstance(art.get("isActive"), bool):
            err("{}: isActive must be a boolean".format(where))
    return seen


def check_songs(doc, artists):
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        err("songs: schemaVersion must be {}".format(SCHEMA_VERSION))
    items = doc.get("songs")
    if not isinstance(items, list) or not items:
        err("songs: songs must be a non-empty array")
        return

    seen = set()
    per_artist = {}
    for i, song in enumerate(items):
        where = "songs[{}]".format(i)
        sid = song.get("id")
        if not isinstance(sid, str) or not SONG_ID.match(sid):
            err("{}: id must match sg_NNNNNN, got {!r}".format(where, sid))
            continue
        if sid in seen:
            err("{}: duplicate song id {!r}".format(where, sid))
        seen.add(sid)

        # Referential integrity. A dangling artistId renders a song with a
        # blank artist, or crashes a naive join.
        aid = song.get("artistId")
        if aid not in artists:
            err("{} ({}): artistId {!r} does not exist in artists.json".format(where, sid, aid))
        elif song.get("isActive") is True:
            per_artist[aid] = per_artist.get(aid, 0) + 1

        nonempty(song, "titleTa", where)
        nonempty(song, "titleEn", where)

        path = song.get("audioPath")
        ver = song.get("audioVersion")
        if not isinstance(path, str) or not ASCII_PATH.match(path or ""):
            err("{}: audioPath must be an ASCII path, got {!r}".format(where, path))
        elif not isinstance(ver, int):
            err("{}: audioVersion must be an integer".format(where))
        elif not path.endswith("_v{}.mp3".format(ver)):
            # The version must live in the filename. Audio is cached immutable
            # for a year at the CDN, so replacing a file in place would serve
            # stale audio with no remote fix.
            err("{}: audioPath must end with _v{}.mp3 to match audioVersion, got {!r}".format(where, ver, path))

        for key in ("durationSec", "fileSizeBytes", "sortOrder"):
            v = song.get(key)
            if not isinstance(v, int) or v <= 0:
                err("{}: {} must be a positive integer".format(where, key))
        if not isinstance(song.get("isActive"), bool):
            err("{}: isActive must be a boolean".format(where))

        for key in ("tagsTa", "tagsEn", "searchAliases"):
            v = song.get(key)
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                err("{}: {} must be an array of strings".format(where, key))

        # Not fatal, but a song with no aliases is effectively unsearchable for
        # anyone typing Tanglish - the highest-value content field there is.
        if not song.get("searchAliases"):
            warn("{} ({}): no searchAliases; Tanglish search will miss this song".format(where, sid))

    featured = sum(1 for s in items
                   if s.get("isFeatured") is True and s.get("isActive") is True)
    if featured == 0:
        # Not fatal: the app falls back to showing every song. But Home is
        # meant to be curated, so silence here is almost always a mistake.
        warn("no songs marked isFeatured; Home will fall back to the full catalog")

    for aid, art in artists.items():
        declared = art.get("songCount")
        actual = per_artist.get(aid, 0)
        if isinstance(declared, int) and declared != actual:
            warn("artists[{}]: songCount={} but {} active songs found "
                 "(advisory - the app computes this itself)".format(aid, declared, actual))


def report():
    # Error messages can quote non-ASCII content (a Tamil filename, say). On a
    # Windows cp1252 console that raises UnicodeEncodeError *while reporting*,
    # swallowing every remaining error. Force UTF-8 with replacement so the
    # validator can always finish saying what is wrong.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    for w in warnings:
        print("WARN  " + w)
    for e in errors:
        print("ERROR " + e)
    print("")
    print("{} error(s), {} warning(s)".format(len(errors), len(warnings)))
    return 1 if errors else 0


def main():
    manifest = load("manifest.json")
    artists_doc = load("artists.json")
    songs_doc = load("songs.json")
    if errors:
        return report()

    check_manifest(manifest)
    artists = check_artists(artists_doc)
    check_songs(songs_doc, artists)
    return report()


if __name__ == "__main__":
    sys.exit(main())
