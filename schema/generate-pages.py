#!/usr/bin/env python3
"""
Generate the landing page and one share page per active song.

Why these are pre-generated and not client-rendered
---------------------------------------------------
A single-page app that reads location.pathname and fetches songs.json works
perfectly in a browser and fails completely where it matters: **WhatsApp and
Facebook crawlers do not execute JavaScript**. Every shared link would preview
with generic site metadata instead of the song. For a share-driven devotional
app in India, where WhatsApp is the primary channel, that is most of the
organic growth gone.

So each song gets a real HTML file with real Open Graph tags.

Usage
-----
    python schema/generate-pages.py            # write the pages
    python schema/generate-pages.py --check    # fail if they are out of date

CI runs --check, so editing songs.json without regenerating is caught at merge
rather than discovered as a stale or missing share page weeks later.
"""

import argparse
import io
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(WEB, "data")
SHARE_DIR = os.path.join(WEB, "s")

SITE = "https://naamaakel.aanmeegasaaral.in"   # NOTE the double 'a'
BRAND_TA = "நாமா கேள்"
BRAND_EN = "Naama Kel"
TAGLINE_TA = "எங்கும் நாமா, எப்போதும் நாமா"
PUBLISHER = "ஆன்மீக சாரல் · Aanmeega Saaral"


def esc(s):
    """Escape for both text nodes and double-quoted attributes."""
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def load(name):
    with io.open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def style():
    """Shared with privacy/ and about/ so the whole site reads as one thing."""
    return """<style>
  :root{
    --bg:#fdfbf7; --surface:#fff; --text:#1c1a17; --muted:#5c554c;
    --accent:#9a3412; --accent-soft:#fdf0e7; --border:#e6ded2;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --bg:#14120f; --surface:#1d1a16; --text:#f0ebe4; --muted:#a8a096;
      --accent:#fb923c; --accent-soft:#2a1c12; --border:#332d26;
    }
  }
  :root[data-theme="dark"]{
    --bg:#14120f; --surface:#1d1a16; --text:#f0ebe4; --muted:#a8a096;
    --accent:#fb923c; --accent-soft:#2a1c12; --border:#332d26;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:var(--bg); color:var(--text);
    font-family:"Noto Sans Tamil","Inter",system-ui,-apple-system,"Segoe UI",sans-serif;
    font-size:18px; line-height:1.75; -webkit-text-size-adjust:100%;
  }
  .wrap{max-width:44rem; margin:0 auto; padding:2rem 1.25rem 5rem}
  header{border-bottom:2px solid var(--border); padding-bottom:1.5rem; margin-bottom:2rem}
  .brand{font-size:1.6rem; font-weight:700; margin:0 0 .25rem; color:var(--accent)}
  .brand a{color:inherit; text-decoration:none}
  .tag{margin:0; color:var(--muted); font-size:1rem}
  h1{font-size:1.9rem; margin:0 0 .35rem; line-height:1.3}
  h2{font-size:1.4rem; margin:2.5rem 0 .75rem}
  .artist{color:var(--muted); font-size:1.15rem; margin:0 0 1.5rem}
  p,li{margin:.6rem 0}
  audio{width:100%; margin:1.25rem 0}
  .cta{
    display:block; text-align:center; padding:1rem 1.25rem; border-radius:999px;
    background:var(--accent); color:#fff; text-decoration:none; font-weight:700;
    margin:1.5rem 0 .75rem; min-height:56px; line-height:1.5;
  }
  .cta.secondary{background:var(--surface); color:var(--text); border:1.5px solid var(--border)}
  .soon{
    display:block; text-align:center; padding:1rem 1.25rem; border-radius:999px;
    background:var(--accent-soft); color:var(--accent); font-weight:600;
    margin:1.5rem 0 .75rem; border:1.5px dashed var(--accent);
  }
  .box{
    background:var(--accent-soft); border-left:4px solid var(--accent);
    padding:1rem 1.15rem; border-radius:.4rem; margin:1.25rem 0;
  }
  .grid{display:grid; gap:.5rem; margin:1.5rem 0}
  .grid a{
    display:block; padding:.85rem 1rem; border:1.5px solid var(--border);
    border-radius:.5rem; text-decoration:none; color:var(--text);
    background:var(--surface); min-height:56px;
  }
  .grid a:hover{border-color:var(--accent)}
  .grid .t{font-weight:600; display:block}
  .grid .a{color:var(--muted); font-size:.95rem}
  footer{margin-top:3rem; padding-top:1.5rem; border-top:1px solid var(--border); color:var(--muted); font-size:.95rem}
  a{color:var(--accent)}
</style>"""


def head(title, description, canonical, og_image=None, audio_url=None):
    tags = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>%s</title>" % esc(title),
        '<meta name="description" content="%s">' % esc(description),
        '<link rel="canonical" href="%s">' % esc(canonical),
        '<link rel="preconnect" href="https://fonts.googleapis.com">',
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
        '<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Tamil:wght@400;600;700'
        '&family=Inter:wght@400;600;700&display=swap" rel="stylesheet">',
        # Open Graph. This block is the entire reason these pages are
        # pre-generated rather than client-rendered.
        '<meta property="og:type" content="%s">' % ("music.song" if audio_url else "website"),
        '<meta property="og:site_name" content="%s">' % esc("%s · %s" % (BRAND_TA, BRAND_EN)),
        '<meta property="og:title" content="%s">' % esc(title),
        '<meta property="og:description" content="%s">' % esc(description),
        '<meta property="og:url" content="%s">' % esc(canonical),
        '<meta property="og:locale" content="ta_IN">',
        '<meta name="twitter:card" content="%s">' % ("summary_large_image" if og_image else "summary"),
    ]
    if og_image:
        tags.append('<meta property="og:image" content="%s">' % esc(og_image))
        tags.append('<meta property="og:image:alt" content="%s">' % esc(title))
    if audio_url:
        tags.append('<meta property="og:audio" content="%s">' % esc(audio_url))
        tags.append('<meta property="og:audio:type" content="audio/mpeg">')
    return "\n".join(tags)


def cta_block(play_url):
    """
    A dead Play link is worse than an honest one.

    Until the listing exists -- which waits on the Play Console conversion to an
    organization account -- there is no URL to send anyone to, so the page says
    so rather than offering a button that 404s.
    """
    if play_url:
        return ('<a class="cta" href="%s">Google Play-யில் நாமா கேள் பெறுங்கள் · '
                'Get Naama Kel on Google Play</a>' % esc(play_url))
    return ('<span class="soon">நாமா கேள் செயலி விரைவில் · '
            'The Naama Kel app is coming soon</span>')


def song_page(song, artist, manifest):
    title_ta = song.get("titleTa") or song.get("titleEn")
    title_en = song.get("titleEn") or song.get("titleTa")
    artist_ta = (artist or {}).get("nameTa") or (artist or {}).get("nameEn") or ""
    artist_en = (artist or {}).get("nameEn") or (artist or {}).get("nameTa") or ""

    audio_url = manifest["audioBaseUrl"] + song["audioPath"]
    photo = (artist or {}).get("photoPath")
    og_image = (manifest.get("imageBaseUrl", "") + photo) if photo else None
    canonical = "%s/s/%s" % (SITE, song["id"])

    page_title = "%s – %s · %s" % (title_ta, artist_ta, BRAND_TA)
    description = ("%s – %s. நாமா கேள் செயலியில் கேளுங்கள், ஒரே தட்டலில் "
                   "அழைப்பு ஒலியாக அமையுங்கள். Listen to %s by %s on Naama Kel and set it "
                   "as your ringtone in one tap." % (title_ta, artist_ta, title_en, artist_en))

    return """<!DOCTYPE html>
<html lang="ta">
<head>
%(head)s
%(style)s
</head>
<body>
<div class="wrap">

<header>
  <p class="brand"><a href="/">%(brand_ta)s · %(brand_en)s</a></p>
  <p class="tag">%(tagline)s</p>
</header>

<h1>%(title_ta)s</h1>
<p class="artist">%(artist_ta)s</p>

<audio controls preload="none" src="%(audio)s">
  உங்கள் உலாவி ஒலியை இயக்க முடியவில்லை. · Your browser cannot play this audio.
</audio>

%(cta)s
<a class="cta secondary" href="/">மற்ற நாமாக்கள் · Browse more Naama</a>

<div class="box">
  <p style="margin:0">செயலி நிறுவப்பட்டிருந்தால், இந்த இணைப்பு நேரடியாக இந்த நாமாவைத் திறக்கும்.<br>
  <span style="color:var(--muted)">If the app is installed, this link opens straight to this Naama.</span></p>
</div>

<h2>%(title_en)s</h2>
<p class="artist">%(artist_en)s</p>

<footer>
  <p>© %(publisher)s ·
     <a href="/about/">பற்றி · About</a> ·
     <a href="/privacy/">தனியுரிமை · Privacy</a></p>
</footer>

</div>
</body>
</html>
""" % {
        "head": head(page_title, description, canonical, og_image, audio_url),
        "style": style(),
        "brand_ta": esc(BRAND_TA), "brand_en": esc(BRAND_EN),
        "tagline": esc(TAGLINE_TA),
        "title_ta": esc(title_ta), "title_en": esc(title_en),
        "artist_ta": esc(artist_ta), "artist_en": esc(artist_en),
        "audio": esc(audio_url),
        "cta": cta_block(manifest.get("playStoreUrl")),
        "publisher": esc(PUBLISHER),
    }


def landing_page(songs, artists, manifest):
    canonical = SITE + "/"
    description = ("தமிழ் நாமாக்களைக் கேளுங்கள், ஒரே தட்டலில் அழைப்பு ஒலியாகவோ "
                   "அலாரமாகவோ அமையுங்கள். Listen to Tamil devotional Naama and set any of "
                   "them as your ringtone or alarm in one tap.")

    by_id = {a["id"]: a for a in artists}
    rows = []
    for s in songs:
        a = by_id.get(s["artistId"], {})
        rows.append(
            '  <a href="/s/%s"><span class="t">%s</span>'
            '<span class="a">%s</span></a>' % (
                esc(s["id"]),
                esc(s.get("titleTa") or s.get("titleEn")),
                esc(a.get("nameTa") or a.get("nameEn") or ""),
            )
        )

    return """<!DOCTYPE html>
<html lang="ta">
<head>
%(head)s
%(style)s
</head>
<body>
<div class="wrap">

<header>
  <p class="brand">%(brand_ta)s · %(brand_en)s</p>
  <p class="tag">%(tagline)s</p>
</header>

<div class="box">
  <p style="margin:0">ஒரு நாமாவைக் கேளுங்கள். பிடித்திருந்தால் ஒரே தட்டலில் அதை உங்கள்
  <strong>அழைப்பு ஒலியாகவோ அலாரமாகவோ</strong> அமைத்துக் கொள்ளுங்கள்.
  ஒருமுறை அமைத்த பிறகு இணையம் இல்லாமலும் வேலை செய்யும்.</p>
</div>

%(cta)s

<h2>நாமாக்கள் · Naama</h2>
<div class="grid">
%(rows)s
</div>

<footer>
  <p>© %(publisher)s ·
     <a href="/about/">பற்றி · About</a> ·
     <a href="/privacy/">தனியுரிமை · Privacy</a></p>
</footer>

</div>
</body>
</html>
""" % {
        "head": head("%s · %s — %s" % (BRAND_TA, BRAND_EN, TAGLINE_TA),
                     description, canonical),
        "style": style(),
        "brand_ta": esc(BRAND_TA), "brand_en": esc(BRAND_EN),
        "tagline": esc(TAGLINE_TA),
        "cta": cta_block(manifest.get("playStoreUrl")),
        "rows": "\n".join(rows) if rows else "  <p>விரைவில் · Coming soon</p>",
        "publisher": esc(PUBLISHER),
    }


def build():
    """{relative path: contents} for every page that should exist."""
    manifest = load("manifest.json")
    artists = [a for a in load("artists.json")["artists"] if a.get("isActive") is True]
    songs = [s for s in load("songs.json")["songs"] if s.get("isActive") is True]
    songs.sort(key=lambda s: (s.get("sortOrder", 0), s["id"]))

    by_id = {a["id"]: a for a in artists}
    pages = {"index.html": landing_page(songs, artists, manifest)}
    for s in songs:
        pages[os.path.join("s", s["id"], "index.html")] = song_page(s, by_id.get(s["artistId"]), manifest)
    return pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the generated pages are missing or stale")
    args = ap.parse_args()

    pages = build()
    stale = []

    for rel, contents in pages.items():
        path = os.path.join(WEB, rel)
        current = None
        if os.path.exists(path):
            with io.open(path, encoding="utf-8") as f:
                current = f.read()
        if current == contents:
            continue
        stale.append(rel + (" (missing)" if current is None else " (out of date)"))
        if not args.check:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(contents)

    # A song that goes inactive must lose its page, or a link shared earlier
    # keeps serving content the app itself refuses to open.
    expected = {os.path.normpath(p) for p in pages}
    orphans = []
    if os.path.isdir(SHARE_DIR):
        for name in sorted(os.listdir(SHARE_DIR)):
            rel = os.path.normpath(os.path.join("s", name, "index.html"))
            if rel not in expected:
                orphans.append(name)
                if not args.check:
                    shutil.rmtree(os.path.join(SHARE_DIR, name), ignore_errors=True)

    if args.check:
        if stale or orphans:
            for s in stale:
                print("STALE   " + s)
            for o in orphans:
                print("ORPHAN  s/%s (song is inactive or removed)" % o)
            print("\nRun: python schema/generate-pages.py")
            return 1
        print("%d page(s) up to date." % len(pages))
        return 0

    print("Wrote %d page(s); %d changed, %d orphan(s) removed."
          % (len(pages), len(stale), len(orphans)))
    for s in stale:
        print("  " + s)
    for o in orphans:
        print("  removed s/" + o)
    return 0


if __name__ == "__main__":
    sys.exit(main())
