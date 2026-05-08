#!/usr/bin/env python3
"""
Jack Hopkins YouTube → Podcast Generator
- Transcripts fetched via yt-dlp
- Summaries via Claude Haiku
- Audio via edge-tts
- MP3s stored in GitHub Releases (no size limit)
- RSS feed + index hosted on GitHub Pages
"""
import os, json, asyncio, subprocess, re, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone

CHANNEL_URL   = "https://www.youtube.com/@JackhopkinsCEO/videos"
VOICE         = "en-GB-RyanNeural"
DOCS_DIR      = Path("docs")
BATCH_SIZE    = int(os.environ.get("BATCH_SIZE", "15"))
START_INDEX   = int(os.environ.get("START_INDEX", "0"))
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GH_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
REPO          = os.environ.get("GITHUB_REPOSITORY", "DanyKamel/voice-memo-tts")
PAGES_URL     = os.environ.get("PAGES_URL", "https://danykamel.github.io/voice-memo-tts")
RELEASE_TAG   = "episodes"
MIN_DURATION  = 180  # skip Shorts (under 3 min)


# ── GitHub API helpers ──────────────────────────────────────────────────────

def gh(method, path, data=None, upload_path=None, content_type="application/json"):
    base = "https://api.github.com" if not upload_path else "https://uploads.github.com"
    url  = f"{base}{path}"
    req  = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"token {GH_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    if upload_path:
        req.add_header("Content-Type", content_type)
        body = Path(upload_path).read_bytes()
    elif data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode()
    else:
        body = None
    try:
        with urllib.request.urlopen(req, body) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        return json.loads(raw) if raw.strip() else {"status": e.code}


def get_or_create_release():
    """Return the 'episodes' release, creating it if needed."""
    r = gh("GET", f"/repos/{REPO}/releases/tags/{RELEASE_TAG}")
    if "id" in r:
        return r
    r = gh("POST", f"/repos/{REPO}/releases", {
        "tag_name": RELEASE_TAG,
        "name": "Jack Hopkins — Episode Summaries",
        "body": "AI-generated MP3 summaries of Jack Hopkins YouTube videos.",
        "draft": False, "prerelease": False
    })
    return r


def upload_asset(release_id, filepath, filename):
    """Upload an MP3 to the release and return its download URL."""
    # Delete existing asset with same name to allow re-upload
    assets = gh("GET", f"/repos/{REPO}/releases/{release_id}/assets")
    if isinstance(assets, list):
        for asset in assets:
            if asset.get("name") == filename:
                gh("DELETE", f"/repos/{REPO}/releases/assets/{asset['id']}")
                break
    url  = f"/repos/{REPO}/releases/{release_id}/assets?name={filename}"
    resp = gh("POST", url, upload_path=filepath, content_type="audio/mpeg")
    return resp.get("browser_download_url", "")


# ── YouTube helpers ─────────────────────────────────────────────────────────

def get_video_list():
    print("Fetching video list (long-form only, no Shorts)...")
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist",
         "--print", "%(id)s\t%(title)s\t%(upload_date)s\t%(duration)s",
         "--no-warnings", CHANNEL_URL],
        capture_output=True, text=True, timeout=180
    )
    videos, skipped = [], 0
    for line in result.stdout.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        vid_id = parts[0].strip()
        title  = parts[1].strip()
        date   = parts[2].strip() if len(parts) > 2 else "20240101"
        dur    = parts[3].strip() if len(parts) > 3 else "0"
        if not vid_id:
            continue
        try:
            duration = int(float(dur))
        except (ValueError, TypeError):
            duration = 0
        if 0 < duration < MIN_DURATION:
            skipped += 1
            continue
        videos.append({"id": vid_id, "title": title, "date": date})
    print(f"  {len(videos)} long-form videos, {skipped} Shorts skipped.")
    return videos


def get_transcript(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
    try:
        entries = YouTubeTranscriptApi.get_transcript(
            video_id, languages=["en", "en-US", "en-GB", "en-AU"]
        )
        text = " ".join(e["text"] for e in entries).strip()
        print(f"  Transcript: {len(text.split())} words")
        return text
    except NoTranscriptFound as e:
        print(f"  No transcript found: {e}")
        return None
    except TranscriptsDisabled as e:
        print(f"  Transcripts disabled: {e}")
        return None
    except Exception as e:
        print(f"  Transcript error ({type(e).__name__}): {e}")
        return None


# ── Summarisation ───────────────────────────────────────────────────────────

def summarize(title, transcript):
    if ANTHROPIC_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                messages=[{"role": "user", "content": (
                    "Summarise this YouTube video as a 180-220 word voice memo. "
                    "Natural spoken English only — no bullet points, no headers. "
                    "Open with one sentence on what the video is about, cover the "
                    "4-6 most useful ideas, end with one actionable takeaway. "
                    "Sound like a smart friend recapping it, not a robot.\n\n"
                    f"Title: {title}\n\nTranscript:\n{transcript[:7000]}"
                )}]
            )
            return msg.content[0].text.strip()
        except Exception as e:
            print(f"  Claude error: {e} — using extractive fallback")
    words = transcript.split()
    return f"Jack Hopkins video: {title}. {' '.join(words[:350])}"


# ── Audio ───────────────────────────────────────────────────────────────────

async def _gen_audio(text, path):
    import edge_tts
    await edge_tts.Communicate(text, VOICE).save(str(path))

def generate_audio(text, path):
    asyncio.run(_gen_audio(text, path))


# ── Manifest + feed ─────────────────────────────────────────────────────────

def safe_filename(index, title):
    clean = re.sub(r"[^\w\s-]", "", title)[:55].strip().replace(" ", "_")
    return f"{index:04d}_{clean}.mp3"

def load_manifest():
    mf = DOCS_DIR / "manifest.json"
    return json.loads(mf.read_text()) if mf.exists() else {"episodes": [], "total_videos": 0}

def save_manifest(m):
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "manifest.json").write_text(json.dumps(m, indent=2))

def build_rss(manifest):
    rss = ET.Element("rss", version="2.0",
                     attrib={"xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"})
    ch  = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text       = "Jack Hopkins — Summaries by Danykamel"
    ET.SubElement(ch, "link").text        = PAGES_URL
    ET.SubElement(ch, "description").text = "AI audio summaries of Jack Hopkins YouTube videos."
    ET.SubElement(ch, "language").text    = "en"
    ET.SubElement(ch, "itunes:author").text = "Danykamel"
    ET.SubElement(ch, "itunes:category", text="Education")
    for ep in reversed(manifest["episodes"]):
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text       = ep["title"]
        ET.SubElement(item, "description").text = f"Summary: {ep['title']}"
        ET.SubElement(item, "enclosure",
                      url=ep["mp3_url"], type="audio/mpeg", length="0")
        ET.SubElement(item, "guid").text    = ep["id"]
        ET.SubElement(item, "pubDate").text = ep.get("date", "")
        ET.SubElement(item, "itunes:duration").text = "3:00"
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(str(DOCS_DIR / "feed.xml"), encoding="unicode", xml_declaration=True)

def build_index(manifest):
    done  = len(manifest["episodes"])
    total = manifest["total_videos"]
    rows  = "".join(
        f'<div class="ep"><strong>{ep["title"]}</strong>'
        f'<audio controls src="{ep["mp3_url"]}"></audio></div>'
        for ep in reversed(manifest["episodes"])
    )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>Jack Hopkins Podcast Summaries</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:820px;margin:40px auto;padding:0 20px;background:#0d0d0d;color:#eee}}
  h1{{color:#fff}} a{{color:#4af}} code{{background:#1a1a1a;padding:3px 7px;border-radius:4px}}
  .ep{{background:#161616;border-radius:8px;padding:14px 18px;margin:10px 0}}
  audio{{width:100%;margin-top:8px}}
</style></head><body>
<h1>🎙 Jack Hopkins — AI Summaries</h1>
<p><strong>{done}</strong> of <strong>{total}</strong> long-form videos processed.</p>
<p>Subscribe in any podcast app: <code>{PAGES_URL}/feed.xml</code>
&nbsp; <a href="feed.xml">📡 RSS</a></p><hr>
{rows}
</body></html>"""
    (DOCS_DIR / "index.html").write_text(html)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    DOCS_DIR.mkdir(exist_ok=True)
    tmp_audio = Path("/tmp/audio")
    tmp_audio.mkdir(exist_ok=True)

    videos  = get_video_list()
    total   = len(videos)

    manifest = load_manifest()
    manifest["total_videos"] = total
    processed_ids = {ep["id"] for ep in manifest["episodes"]}

    print(f"\nGetting GitHub release...")
    release    = get_or_create_release()
    release_id = release.get("id")
    if not release_id:
        print(f"ERROR: Could not get/create release: {release}")
        return

    batch = videos[START_INDEX: START_INDEX + BATCH_SIZE]
    print(f"Processing videos {START_INDEX+1}–{START_INDEX+len(batch)} of {total}\n")

    new_count = 0
    for i, video in enumerate(batch):
        vid_id = video["id"]
        title  = video["title"]
        idx    = START_INDEX + i + 1
        fname  = safe_filename(idx, title)
        mp3    = tmp_audio / fname

        print(f"[{idx}/{total}] {title}")
        if vid_id in processed_ids:
            print("  Already processed, skipping.\n")
            continue

        print("  Transcript...")
        transcript = get_transcript(vid_id)
        if not transcript:
            print("  No transcript — skipping.\n")
            continue

        print("  Summarising...")
        summary = summarize(title, transcript)

        print("  Generating audio...")
        generate_audio(summary, mp3)

        print("  Uploading to GitHub Releases...")
        mp3_url = upload_asset(release_id, str(mp3), fname)
        if not mp3_url:
            print("  Upload failed — skipping.\n")
            continue

        mp3.unlink(missing_ok=True)

        date_str = video.get("date", "20240101")
        try:
            dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
            pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            pub_date = ""

        manifest["episodes"].append({
            "id": vid_id, "title": title,
            "file": fname, "mp3_url": mp3_url, "date": pub_date
        })
        new_count += 1
        print(f"  Done → {mp3_url}\n")

    manifest["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_manifest(manifest)
    build_rss(manifest)
    build_index(manifest)

    print(f"\n✅ {new_count} new episodes. {len(manifest['episodes'])} total.")
    print(f"Feed: {PAGES_URL}/feed.xml")


if __name__ == "__main__":
    main()
