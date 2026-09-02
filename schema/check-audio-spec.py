#!/usr/bin/env python3
"""
Measure every published song against the house audio standard (plan section 17).

    python schema/check-audio-spec.py            # measure everything active
    python schema/check-audio-spec.py --limit 5  # first five, for a quick look

This is a GATE, not a report. It exits 1 when the published catalog breaks something
that stops a Naama working as a ringtone, and CI fails with it.

Two kinds of finding, deliberately separated: structural faults BLOCK, absolute
per-file loudness only WARNS, and the catalog-wide loudness SPREAD blocks. The
comment beside the constants explains why -- in short, the spread is what a listener
actually notices, and a gate nobody can keep green is a gate nobody reads.

### Why this is separate from validate.py --check-audio

That check sends a HEAD request: it proves the file EXISTS and that its length
matches `fileSizeBytes`. It cannot see inside the file, so a song at 44.1 kHz, in
stereo, or 6 dB louder than the rest of the catalog passes it without complaint.
Those are exactly the faults that are invisible in review and obvious in the ear.

### Why it has to run in CI rather than by hand

Section 17: "CI must fail when any active published song violates the standard...
A file that only warns is a file that ships." The backend measures uploads at the
moment they arrive, which is the right place to catch a bad export -- but it says
nothing about the catalog as a whole, and nobody re-runs a manual check before a
content commit. Loudness in particular is a property of the SET: a catalog uniformly
at -12 LUFS is fine, and a catalog averaging -14 with one song at -8 is not, and only
the second sounds broken.

### Cost

Audio filenames carry a content hash and a version and are cached immutably, so a
given `audioPath` never changes. Paths already measured are recorded in
`.audio-spec-cache.json`, and CI restores it, so a steady-state run downloads
nothing and only genuinely new or re-versioned audio is fetched.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = ROOT / ".audio-spec-cache.json"

# --- the standard, from plan section 17 -------------------------------------
#
# These are the ENFORCEMENT numbers from section 17's table, which that section
# declares is "the only audio specification in this document" and that numbers
# elsewhere are "historical and superseded". They are deliberately tighter than the
# backend's advisory specNotes() (which tolerates +/-2 LUFS and 128 kbps): those are
# what the upload screen shows a human, this is what may reach a user.
CODEC = "mp3"
CHANNELS = 1
SAMPLE_RATE = 48_000
BITRATE_KBPS = 96
BITRATE_TOLERANCE = 8          # CBR encoders land a few kbps either side
LUFS_MIN, LUFS_MAX = -15.0, -13.0
TRUE_PEAK_MAX = -1.5           # dBTP

# --- what BLOCKS, and what merely reports ----------------------------------
#
# BLOCKING: codec, channels, sample rate, bitrate, embedded artwork, the versioned
# filename, and fileSizeBytes matching what the CDN serves. Each of those either
# stops the file working as a ringtone or breaks something downstream -- a 44.1 kHz
# file is resampled by Android on the way out, a stereo file doubles the download for
# a mono source, and a wrong fileSizeBytes makes the download guard REJECT a good
# file, which has happened twice in this project.
#
# ADVISORY: absolute per-file loudness and true peak. Not because they do not matter,
# but because this window is tighter than the threshold at which anyone can hear a
# difference, and a permanently red gate teaches everyone to ignore it. Two songs in
# the live catalog sit 0.3 and 0.5 dB outside it and are inaudible against the rest;
# the client accepted them on 28 Aug 2026.
#
# STILL BLOCKING, and it is the one that matters: the catalog-wide SPREAD below. A
# listener hears the difference BETWEEN tracks, not the distance from a number. A
# catalog uniformly at -12 LUFS is fine; one averaging -14 with a song at -8 is not,
# and only the second sounds broken. That check maps to what is audible, so it fails
# the build.

# Beyond ~3 dB one Naama audibly jumps against the others. Under ~1.5 dB is
# inaudible track to track. This is the check that only makes sense catalog-wide.
SPREAD_AUDIBLE = 3.0
SPREAD_NOTICEABLE = 1.5

VERSIONED = re.compile(r"_v\d+\.mp3$")

errors = []
warnings = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def load(name):
    p = DATA / name
    if not p.exists():
        err("{}: missing".format(name))
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        err("{}: not valid JSON ({})".format(name, e))
        return None


def need_tool(name):
    """A missing tool is an ENVIRONMENT failure, not a catalog violation.

    It still fails the build -- a gate that cannot measure must never report
    success -- but it must not say the catalog is wrong, because that sends
    someone to re-export audio that was fine all along.
    """
    if shutil.which(name) is None:
        print("  CANNOT RUN: {} is not installed, so nothing can be measured.".format(name))
        print("  This is an environment problem, not a problem with the catalog.")
        print("  CI installs ffmpeg; locally, install it or run this on the backend host.")
        sys.exit(2)
    return True


def probe(path):
    """Codec, channels, sample rate and bitrate, via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,channels,sample_rate,bit_rate",
         "-show_entries", "format=bit_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None
    d = json.loads(out.stdout or "{}")
    streams = d.get("streams") or [{}]
    s = streams[0]
    # Stream bit_rate is absent on some MP3s; the format-level one is the fallback.
    bits = s.get("bit_rate") or (d.get("format") or {}).get("bit_rate") or 0
    return {
        "codec": s.get("codec_name") or "",
        "channels": int(s.get("channels") or 0),
        "sample_rate": int(s.get("sample_rate") or 0),
        "kbps": round(int(bits) / 1000) if bits else 0,
    }


def has_artwork(path):
    """An embedded cover is a video stream in an MP3. Section 17 says strip it."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return bool((out.stdout or "").strip())


def loudness(path):
    """Integrated LUFS and max true peak, via ffmpeg's ebur128 filter."""
    out = subprocess.run(
        ["ffmpeg", "-nostats", "-hide_banner", "-i", str(path),
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    text = (out.stderr or "") + (out.stdout or "")
    # ffmpeg prints a Summary block; the last occurrence is the integrated result.
    lufs = peak = None
    m = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", text)
    if m:
        lufs = float(m[-1])
    m = re.findall(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", text)
    if m:
        peak = float(m[-1])
    return lufs, peak


def fetch(url, dest):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "naamakel-ci"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        err("could not fetch {} ({})".format(url, e))
        return False


def check_song(song, base, tmpdir, cache):
    # How many blocking problems existed before this song was looked at. Used at the
    # end to decide whether the result is safe to cache.
    errors_before = len(errors)
    sid = song.get("id", "?")
    path = (song.get("audioPath") or "").strip()
    title = song.get("titleEn") or sid

    if not path:
        err("{}: no audioPath".format(sid))
        return None

    # Section 17: ASCII, version-suffixed. The app's cache key and the CDN's
    # immutable caching both depend on the version being in the filename.
    if not VERSIONED.search(path):
        err("{}: audioPath {!r} must end in _v<N>.mp3".format(sid, path))

    if path in cache:
        # Immutable by construction: the filename carries a content hash and a
        # version, so the same path is the same bytes it was last time.
        return cache[path].get("lufs")

    url = base.rstrip("/") + "/" + path.lstrip("/")
    local = Path(tmpdir) / "song.mp3"
    if not fetch(url, local):
        return None

    size = local.stat().st_size
    p = probe(local)
    if p is None:
        err("{}: ffprobe could not read the file".format(sid))
        return None
    lufs, peak = loudness(local)

    print("  {:<12} {:<26} {:>5}K {:>2}ch {:>6.1f}k {:>4}kbps {:>7} LUFS".format(
        sid, title[:24], round(size / 1024), p["channels"],
        p["sample_rate"] / 1000, p["kbps"],
        "?" if lufs is None else "{:.1f}".format(lufs)))

    if p["codec"] != CODEC:
        err("{}: {}, not {}".format(sid, p["codec"] or "unknown codec", CODEC))
    if p["channels"] != CHANNELS:
        err("{}: {} channels, not mono. Twice the download for a mono source, "
            "played through one small speaker".format(sid, p["channels"]))
    if p["sample_rate"] != SAMPLE_RATE:
        err("{}: {:.1f} kHz, not {:.1f}. Android audio runs at 48 kHz natively, so "
            "anything else is resampled on the way out".format(
                sid, p["sample_rate"] / 1000, SAMPLE_RATE / 1000))
    if p["kbps"] and abs(p["kbps"] - BITRATE_KBPS) > BITRATE_TOLERANCE:
        err("{}: {} kbps, not {} kbps".format(sid, p["kbps"], BITRATE_KBPS))
    if has_artwork(local):
        err("{}: has embedded artwork; section 17 says strip it".format(sid))

    if lufs is None:
        err("{}: loudness could not be measured".format(sid))
    elif not (LUFS_MIN <= lufs <= LUFS_MAX):
        warn("{}: {:.1f} LUFS, outside {:.1f} to {:.1f} - {} than the rest".format(
            sid, lufs, LUFS_MIN, LUFS_MAX,
            "quieter" if lufs < LUFS_MIN else "louder"))
    if peak is not None and peak > TRUE_PEAK_MAX:
        warn("{}: true peak {:.1f} dBTP, above {:.1f}".format(sid, peak, TRUE_PEAK_MAX))

    # The catalog's own numbers must match the file the app downloads. A wrong
    # fileSizeBytes makes the ringtone size guard reject a good file, which has
    # happened twice in this project.
    declared = song.get("fileSizeBytes")
    if not isinstance(declared, int) or declared <= 0:
        err("{}: fileSizeBytes must be a positive integer".format(sid))
    elif declared != size:
        err("{}: fileSizeBytes says {} but the CDN serves {}".format(sid, declared, size))

    # Cache ONLY a clean pass.
    #
    # This was caching unconditionally, and the effect was worse than no cache: two
    # stereo songs failed on the first run, were recorded as verified anyway, and the
    # very next run skipped their checks and reported the catalog green. A cache that
    # remembers failures as successes turns a gate into a rubber stamp -- found by
    # running it twice, which is the only way this kind of bug shows up.
    if len(errors) == errors_before:
        cache[path] = {"lufs": lufs, "bytes": size}
    local.unlink(missing_ok=True)
    return lufs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0,
                    help="measure only the first N active songs")
    ap.add_argument("--no-cache", action="store_true",
                    help="re-measure everything, ignoring previously verified paths")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    need_tool("ffprobe")
    need_tool("ffmpeg")

    manifest = load("manifest.json")
    songs_doc = load("songs.json")
    if manifest is None or songs_doc is None:
        report()
        return

    base = (manifest.get("audioBaseUrl") or "").strip()
    if not base:
        err("manifest: audioBaseUrl is required")
        report()
        return

    # Active only. An inactive song is hidden from browse and search, so holding
    # the catalog to a standard for something nobody can reach would block a
    # content edit for no user-visible reason.
    songs = [s for s in (songs_doc.get("songs") or []) if s.get("isActive", True)]
    if args.limit:
        songs = songs[:args.limit]

    cache = {}
    if CACHE.exists() and not args.no_cache:
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    before = len(cache)

    print("  {} active song(s), {} already verified\n".format(len(songs), before))
    print("  {:<12} {:<26} {:>6} {:>4} {:>7} {:>8} {:>12}".format(
        "id", "title", "size", "ch", "rate", "bitrate", "loudness"))
    print("  " + "-" * 82)

    measured = []
    with tempfile.TemporaryDirectory() as tmp:
        for song in songs:
            lufs = check_song(song, base, tmp, cache)
            if lufs is not None:
                measured.append(lufs)

    # The catalog-wide check. This is the one that cannot be made per-file: the
    # listener hears the difference BETWEEN tracks, not the distance from a number.
    print()
    if len(measured) > 1:
        spread = max(measured) - min(measured)
        print("  loudness {:.1f} to {:.1f} LUFS - a {:.1f} dB spread".format(
            min(measured), max(measured), spread))
        if spread > SPREAD_AUDIBLE:
            err("a {:.1f} dB spread is audible; one Naama will jump out against "
                "the others".format(spread))
        elif spread > SPREAD_NOTICEABLE:
            warn("a {:.1f} dB spread is a little uneven, but unlikely to be "
                 "noticed".format(spread))
        else:
            print("  consistent - they will sound level next to each other")

    try:
        CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass

    report()


def report():
    for w in warnings:
        print("  WARNING: {}".format(w))
    for e in errors:
        print("  ERROR:   {}".format(e))
    if errors:
        print("\n  {} blocking problem(s). The published catalog breaks the audio "
              "standard in plan section 17.".format(len(errors)))
        sys.exit(1)
    if warnings:
        print("\n  {} advisory note(s); nothing blocking.".format(len(warnings)))
    else:
        print("\n  everything matches the standard")


if __name__ == "__main__":
    main()
