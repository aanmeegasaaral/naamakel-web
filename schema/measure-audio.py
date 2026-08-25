#!/usr/bin/env python3
"""
Measure the REAL duration and byte size of each song's audio, and rewrite those
fields in songs.json.

Why this exists
---------------
songs.json shipped with placeholder `durationSec` and `fileSizeBytes` from the
seed catalog. Nobody updated them when the real MP3s were uploaded, and the drift
was invisible until it broke something: the app refuses a ringtone download whose
payload is under a quarter of the declared size (a truncated-file guard), so a
song declaring 921 KB while actually being 181 KB could not be set as a ringtone
at all. The error the user saw was a generic "could not download".

So these two fields are not cosmetic. Derive them from the files, never by hand.

Usage
-----
    python schema/measure-audio.py               # report only
    python schema/measure-audio.py --write       # rewrite songs.json

No third-party dependencies: ffprobe and mutagen are not assumed to be present.
"""

import argparse
import io
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# schema/ sits beside data/ in naamakel-web.
WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- MPEG audio frame tables (Layer III only; MP3 is all we ship) -----------

BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
BITRATES_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
SAMPLE_RATES = {
    3: [44100, 48000, 32000, 0],   # MPEG 1
    2: [22050, 24000, 16000, 0],   # MPEG 2
    0: [11025, 12000, 8000, 0],    # MPEG 2.5
}
SAMPLES_PER_FRAME = {3: 1152, 2: 576, 0: 576}


def id3_size(data):
    """Bytes to skip before the first MPEG frame."""
    if len(data) < 10 or data[0:3] != b"ID3":
        return 0
    flags = data[5]
    # Synchsafe: 7 bits per byte.
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    total = 10 + size
    if flags & 0x10:          # footer present
        total += 10
    return total


def parse_frame(data, i):
    """Decode a frame header at offset i. Returns None if it is not one."""
    if i + 4 > len(data):
        return None
    if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
        return None

    version = (data[i + 1] >> 3) & 0x03   # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    layer = (data[i + 1] >> 1) & 0x03     # 1 = Layer III
    if version == 1 or layer != 1:
        return None

    bitrate_idx = (data[i + 2] >> 4) & 0x0F
    rate_idx = (data[i + 2] >> 2) & 0x03
    padding = (data[i + 2] >> 1) & 0x01
    channel_mode = (data[i + 3] >> 6) & 0x03

    table = BITRATES_V1_L3 if version == 3 else BITRATES_V2_L3
    bitrate = table[bitrate_idx] * 1000
    sample_rate = SAMPLE_RATES[version][rate_idx]
    if not bitrate or not sample_rate:
        return None

    spf = SAMPLES_PER_FRAME[version]
    length = int(spf / 8 * bitrate / sample_rate) + padding
    if length <= 4:
        return None

    return {
        "version": version,
        "bitrate": bitrate,
        "sample_rate": sample_rate,
        "samples_per_frame": spf,
        "length": length,
        "mono": channel_mode == 3,
    }


def xing_frame_count(data, frame_start, frame):
    """
    Frame count from a Xing/Info header, when present.

    VBR files cannot be measured from the first frame's bitrate -- that only
    describes the first frame. The Xing header carries the true total.
    """
    if frame["version"] == 3:
        offset = 17 if frame["mono"] else 32
    else:
        offset = 9 if frame["mono"] else 17

    p = frame_start + 4 + offset
    if p + 8 > len(data):
        return None
    if data[p:p + 4] not in (b"Xing", b"Info"):
        return None

    flags = int.from_bytes(data[p + 4:p + 8], "big")
    if not flags & 0x0001:            # frames field absent
        return None
    q = p + 8
    if q + 4 > len(data):
        return None
    return int.from_bytes(data[q:q + 4], "big")


def measure(data):
    """(duration_seconds, kbps, vbr) for an MP3 held in memory."""
    start = id3_size(data)

    # Resync: a byte-exact tag size is not guaranteed in the wild.
    i = start
    frame = None
    limit = min(len(data), start + 200_000)
    while i < limit:
        frame = parse_frame(data, i)
        if frame:
            break
        i += 1
    if not frame:
        raise ValueError("no MPEG Layer III frame found")

    count = xing_frame_count(data, i, frame)
    if count:
        duration = count * frame["samples_per_frame"] / frame["sample_rate"]
        return duration, round(len(data) * 8 / duration / 1000), True

    # No Xing header. Walk the frames so a VBR file without one is still
    # measured correctly rather than assuming the first frame's bitrate.
    frames = 0
    total_bits = 0
    j = i
    while j < len(data):
        f = parse_frame(data, j)
        if not f:
            j += 1
            continue
        frames += 1
        total_bits += f["length"] * 8
        j += f["length"]

    if not frames:
        raise ValueError("no frames could be walked")
    duration = frames * frame["samples_per_frame"] / frame["sample_rate"]
    return duration, round(total_bits / duration / 1000), False


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="rewrite songs.json")
    args = ap.parse_args()

    songs_path = os.path.join(WEB, "data", "songs.json")
    manifest = json.load(io.open(os.path.join(WEB, "data", "manifest.json"), encoding="utf-8"))
    doc = json.load(io.open(songs_path, encoding="utf-8"))
    base = manifest["audioBaseUrl"]

    changed = 0
    skipped = []

    print("%-12s %-26s %9s %9s  %8s %8s" % ("id", "title", "size", "was", "dur", "was"))
    print("-" * 82)

    for song in doc["songs"]:
        url = base + song["audioPath"]
        try:
            data = fetch(url)
        except Exception as e:
            skipped.append((song["id"], song["titleEn"], str(e)[:44]))
            continue

        try:
            duration, kbps, vbr = measure(data)
        except ValueError as e:
            skipped.append((song["id"], song["titleEn"], "not parseable: %s" % e))
            continue

        size = len(data)
        secs = int(round(duration))
        old_size, old_secs = song.get("fileSizeBytes", 0), song.get("durationSec", 0)

        flag = ""
        if size != old_size or secs != old_secs:
            flag = "  <-- CHANGED"
            changed += 1

        print("%-12s %-26s %8.0fK %8.0fK  %7ds %7ds %s%s" % (
            song["id"], song["titleEn"][:26], size / 1024, old_size / 1024,
            secs, old_secs, "VBR " if vbr else "", flag))

        song["fileSizeBytes"] = size
        song["durationSec"] = secs

    if skipped:
        print("\nSKIPPED (audio not reachable -- metadata left untouched):")
        for i, t, e in skipped:
            print("  %-12s %-26s %s" % (i, t[:26], e))

    if args.write and changed:
        # Keep the file's existing shape: 2-space indent, UTF-8, trailing newline.
        with io.open(songs_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("\nWROTE %s (%d song(s) updated)" % (songs_path, changed))
    elif args.write:
        print("\nNothing to write; every reachable song already matches.")
    else:
        print("\n%d song(s) would change. Re-run with --write to apply." % changed)


if __name__ == "__main__":
    main()
