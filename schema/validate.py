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
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
SCHEMA_VERSION = 1

ART_ID = re.compile(r"^ar_[a-z0-9_]+$")
SONG_ID = re.compile(r"^sg_\d{6}$")
CAT_ID = re.compile(r"^cat_[a-z0-9_]+$")
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


def title_key(title):
    """What makes two records the same Naama: the English title, case and
    whitespace ignored.

    Must stay identical to Catalog::titleKey() in naamakel-backend. The two
    guard the same rule at different moments -- the admin refuses to create a
    duplicate, this refuses to publish one -- and if they disagreed, the admin
    would happily save something CI then rejects, with the operator holding a
    correctly typed record and no way to ship it.

    Deliberately not fuzzy. It decides whether legitimate work is refused, and a
    false positive there is worse than a miss.
    """
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def check_manifest(m):
    # Without this the app has no way to fetch categories, and every song would
    # reference an id it can never resolve.
    if not isinstance(m.get("categoriesUrl"), str) or not m["categoriesUrl"].startswith("https://"):
        err("manifest: categoriesUrl must be an https URL")
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


def check_songs(doc, artists, categories):
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

        # Same rule for the category. A typo here files the song into a
        # category that does not exist, so it vanishes from browse while still
        # appearing in search -- confusing, and invisible without this check.
        cid = song.get("categoryId")
        if cid is None:
            err("{} ({}): categoryId is required".format(where, sid))
        elif cid not in categories:
            err("{} ({}): categoryId {!r} is not an active category".format(where, sid, cid))
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

    # ONE ARTIST, ONE RECORDING OF A NAAMA.
    #
    # The same Naama from several singers is expected and allowed -- that is
    # what a version is, and the app lists them as separate rows naming their
    # artists. But the ARTIST is the only thing distinguishing one version from
    # another, so two records sharing a title and an artist are the same
    # recording published twice, with nothing left to tell them apart.
    #
    # They also collide where it hurts: the app derives each exported tone's
    # filename from exactly this pair, and MediaStore deletes by display name
    # before inserting, so a user who sets the second loses the first one's file
    # with no error anywhere.
    #
    # ACTIVE songs only, on purpose. An inactive draft reaches neither a phone
    # nor the bundled seed -- the app's own validator filters on isActive -- so
    # failing the build over two drafts would block publishing to protect
    # nobody. The admin form refuses to create them at all, which is where that
    # mistake is cheap to fix. Same split as the categories rules: CI guards
    # what reaches a user, the form guards the person typing.
    versions = {}
    for i, song in enumerate(items):
        if song.get("isActive") is not True:
            continue
        title, aid = song.get("titleEn"), song.get("artistId")
        if not isinstance(title, str) or not isinstance(aid, str):
            continue
        key = (title_key(title), aid)
        if key[0] == "":
            continue
        if key in versions:
            err("songs[{}] ({}): {!r} is already an active recording by {!r} ({}). "
                "One artist records a Naama once -- a second version needs a "
                "different artist. Both of these also export to the same ringtone "
                "filename, so setting one on a phone deletes the other's file."
                .format(i, song.get("id"), title, aid, versions[key]))
        else:
            versions[key] = song.get("id")

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


def check_audio(manifest, songs_doc, artists_doc):
    """
    Confirm the metadata describes files that actually exist.

    Added after a real failure. songs.json carried the seed catalog's
    placeholder `fileSizeBytes` and `durationSec`, which nobody updated when the
    real MP3s were uploaded. The app refuses a ringtone download whose payload is
    under a quarter of the declared size -- a guard against truncated files -- so
    a song declaring 921 KB while actually being 181 KB simply could not be set
    as a ringtone. The user saw a generic download error and nothing anywhere
    explained why.

    Everything else in this file is a consistency check the data can satisfy on
    its own. This is the only one that asks whether the data matches REALITY, so
    it is the only one that needs the network -- hence the opt-in flag, with CI
    turning it on.

    ONLY ACTIVE SONGS ARE CHECKED. `isActive: false` is the supported way to
    stage a song whose audio has not been uploaded yet, and this check must not
    take that escape hatch away.
    """
    base = manifest.get("audioBaseUrl", "")
    image_base = manifest.get("imageBaseUrl", "")
    if not base:
        err("manifest: audioBaseUrl is required for --check-audio")
        return

    for song in songs_doc.get("songs", []):
        if song.get("isActive") is not True:
            continue
        sid = song.get("id")
        url = base + song.get("audioPath", "")
        declared = song.get("fileSizeBytes")

        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=30) as r:
                actual = int(r.headers.get("Content-Length") or 0)
        except urllib.error.HTTPError as e:
            # The server gave a definitive answer: this object is not servable.
            # An active song users can see but cannot play is exactly the kind of
            # bad commit this validator exists to stop.
            err("songs[{}]: audio not reachable ({} {}) at {}".format(sid, e.code, e.reason, url))
            continue
        except Exception as e:
            # Could not reach the CDN at all. That is inconclusive -- a network
            # blip must not fail an unrelated content commit -- so warn instead
            # of blocking the merge.
            warn("songs[{}]: could not check audio ({}); skipped".format(sid, e))
            continue

        if not isinstance(declared, int) or declared <= 0:
            err("songs[{}]: fileSizeBytes must be a positive integer".format(sid))
        elif actual and actual != declared:
            err("songs[{}]: fileSizeBytes is {} but the file is {} bytes. "
                "Run schema/measure-audio.py --write rather than editing by hand"
                .format(sid, declared, actual))

        dur = song.get("durationSec")
        if not isinstance(dur, int) or dur <= 0:
            err("songs[{}]: durationSec must be a positive integer".format(sid))

    # Artist photos are optional, but a path that 404s renders a broken image
    # rather than the generated-initial fallback, which is worse than having no
    # photo at all.
    for artist in artists_doc.get("artists", []):
        if artist.get("isActive") is not True:
            continue
        path = artist.get("photoPath")
        if not path:
            continue
        url = image_base + path
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30):
                pass
        except urllib.error.HTTPError as e:
            err("artists[{}]: photoPath not reachable ({} {}) at {}".format(
                artist.get("id"), e.code, e.reason, url))
        except Exception as e:
            warn("artists[{}]: could not check photo ({}); skipped".format(artist.get("id"), e))


def check_categories(doc):
    """
    Categories carry their own bilingual names, so a song can be filed once and
    displayed correctly in either language. Returns {id: name} for the ACTIVE
    ones, which is what song validation checks against -- filing a song into a
    deactivated category would hide it from browse with no error anywhere.
    """
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        err("categories: schemaVersion must be {}".format(SCHEMA_VERSION))
    items = doc.get("categories")
    if not isinstance(items, list) or not items:
        err("categories: categories must be a non-empty array")
        return {}

    active = {}
    seen = set()
    for i, cat in enumerate(items):
        where = "categories[{}]".format(i)
        cid = cat.get("id")
        if not isinstance(cid, str) or not CAT_ID.match(cid):
            err("{}: id must match cat_<lowercase>, got {!r}".format(where, cid))
            continue
        if cid in seen:
            err("{}: duplicate category id {!r}".format(where, cid))
        seen.add(cid)

        # BOTH names are mandatory. A missing one leaves a blank chip in that
        # language rather than falling back, because there is nothing to fall
        # back to.
        nonempty(cat, "nameTa", where)
        nonempty(cat, "nameEn", where)

        if not isinstance(cat.get("sortOrder"), int):
            err("{} ({}): sortOrder must be an integer".format(where, cid))

        if cat.get("isActive") is True:
            active[cid] = cat.get("nameEn")

    if not active:
        err("categories: at least one category must be active")
    return active


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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check-audio",
        action="store_true",
        help="also verify every active song's audio exists and matches its "
             "declared size (needs network; CI enables this)",
    )
    args = ap.parse_args()

    manifest = load("manifest.json")
    artists_doc = load("artists.json")
    songs_doc = load("songs.json")
    categories_doc = load("categories.json")
    if errors:
        return report()

    check_manifest(manifest)
    artists = check_artists(artists_doc)
    categories = check_categories(categories_doc)
    check_songs(songs_doc, artists, categories)
    if args.check_audio:
        check_audio(manifest, songs_doc, artists_doc)
    return report()


if __name__ == "__main__":
    sys.exit(main())
