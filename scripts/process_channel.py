#!/usr/bin/env python3
"""
Jack Hopkins YouTube → Podcast Generator
Fetches transcripts, summarises with Claude Haiku, generates MP3 with edge-tts.
"""
import os
import sys
import json
import asyncio
import subprocess
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone

CHANNEL_URL = "https://www.youtube.com/@JackhopkinsCEO/videos"
VOICE = "en-GB-RyanNeural"
OUTPUT_DIR = Path("output")
DOCS_DIR = Path("docs")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "15"))
START_INDEX = int(os.environ.get("START_INDEX", "0"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PAGES_URL = os.environ.get("PAGES_URL", "https://danykamel.github.io/voice-memo-tts")


MIN_DURATION_SECS = 180  # ignore anything under 3 minutes (filters out Shorts)


def get_video_list():
    print("Fetching video list from channel (long-form only, no Shorts)...")
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist",
         "--print", "%(id)s\t%(title)s\t%(upload_date)s\t%(duration)s",
         "--no-warnings", CHANNEL_URL],
        capture_output=True, text=True, timeout=180
    )
    videos = []
    skipped = 0
    for line in result.stdout.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        vid_id = parts[0].strip()
        title = parts[1].strip()
        date = parts[2].strip() if len(parts) > 2 else "20240101"
        duration_str = parts[3].strip() if len(parts) > 3 else "0"
        if not vid_id:
            continue
        try:
            duration = int(float(duration_str))
        except (ValueError, TypeError):
            duration = 0
        if duration > 0 and duration < MIN_DURATION_SECS:
            skipped += 1
            continue  # skip Shorts and very short videos
        videos.append({"id": vid_id, "title": title, "date": date})
    print(f"  {len(videos)} long-form videos found, {skipped} Shorts skipped.")
    return videos


def get_transcript(video_id):
    tmp_path = f"/tmp/yt_{video_id}"
    subprocess.run(
        ["yt-dlp", "--write-auto-subs", "--sub-lang", "en",
         "--sub-format", "vtt", "--skip-download",
         "--output", tmp_path, f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True, timeout=90
    )
    vtt_files = list(Path("/tmp").glob(f"yt_{video_id}*.vtt"))
    if not vtt_files:
        return None
    vtt = vtt_files[0].read_text(encoding="utf-8", errors="ignore")
    # Strip VTT markup to plain text
    lines, seen, prev = [], set(), None
    for line in vtt.split("\n"):
        line = line.strip()
        if (not line or line.startswith("WEBVTT") or "-->" in line
                or re.match(r"^\d+$", line) or re.match(r"^\d{2}:\d{2}", line)):
            continue
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if clean and clean != prev:
            lines.append(clean)
            prev = clean
    # Clean up temp files
    for f in vtt_files:
        f.unlink(missing_ok=True)
    return " ".join(lines)


def summarize_with_claude(title, transcript):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": (
                f"Summarise this YouTube video as a 180-220 word voice memo. "
                f"Write in natural spoken English — no bullet points, no headers. "
                f"Start with a one-sentence hook about what the video is about, "
                f"then cover the 4-6 most useful ideas. End with one actionable takeaway. "
                f"Sound like a smart friend recapping a video, not a robot.\n\n"
                f"Title: {title}\n\nTranscript:\n{transcript[:7000]}"
            )
        }]
    )
    return msg.content[0].text.strip()


def summarize_extractive(title, transcript):
    """Fallback: grab opening + key sentences when no API key."""
    words = transcript.split()
    snippet = " ".join(words[:350])
    return f"This video by Jack Hopkins is titled: {title}. Here are the key points. {snippet}"


def summarize(title, transcript):
    if ANTHROPIC_API_KEY:
        try:
            return summarize_with_claude(title, transcript)
        except Exception as e:
            print(f"  Claude API error: {e} — falling back to extractive")
    return summarize_extractive(title, transcript)


async def generate_audio(text, path):
    import edge_tts
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(str(path))


def safe_filename(index, title):
    clean = re.sub(r"[^\w\s-]", "", title)[:55].strip().replace(" ", "_")
    return f"{index:04d}_{clean}.mp3"


def load_manifest():
    mf = DOCS_DIR / "manifest.json"
    if mf.exists():
        return json.loads(mf.read_text())
    return {"episodes": [], "total_videos": 0, "last_updated": ""}


def save_manifest(manifest):
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))


def build_rss(manifest):
    rss = ET.Element("rss", version="2.0", attrib={
        "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"
    })
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Jack Hopkins — Summaries by Danykamel"
    ET.SubElement(channel, "link").text = PAGES_URL
    ET.SubElement(channel, "description").text = (
        "AI-generated audio summaries of Jack Hopkins' YouTube videos. "
        "Built for Danykamel's personal brand research."
    )
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "itunes:author").text = "Danykamel"
    ET.SubElement(channel, "itunes:category", text="Education")

    for ep in reversed(manifest["episodes"]):  # newest first
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = ep["title"]
        ET.SubElement(item, "description").text = f"Summary of Jack Hopkins' video: {ep['title']}"
        mp3_url = f"{PAGES_URL}/audio/{ep['file']}"
        ET.SubElement(item, "enclosure", url=mp3_url, type="audio/mpeg", length="0")
        ET.SubElement(item, "guid").text = ep["id"]
        ET.SubElement(item, "pubDate").text = ep.get("date", "")
        ET.SubElement(item, "itunes:duration").text = "3:00"

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    rss_path = DOCS_DIR / "feed.xml"
    tree.write(str(rss_path), encoding="unicode", xml_declaration=True)
    return rss_path


def build_index(manifest):
    total = manifest["total_videos"]
    done = len(manifest["episodes"])
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Jack Hopkins Podcast — Summaries</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; background: #0d0d0d; color: #eee; }}
    h1 {{ color: #fff; }} a {{ color: #4af; }}
    .badge {{ background: #222; border-radius: 8px; padding: 12px 20px; margin: 8px 0; display: flex; align-items: center; gap: 16px; }}
    audio {{ width: 100%; margin-top: 8px; }}
    .meta {{ font-size: 0.85em; color: #888; }}
  </style>
</head>
<body>
  <h1>🎙 Jack Hopkins — AI Summaries</h1>
  <p><strong>{done}</strong> of <strong>{total}</strong> videos processed.
  <a href="feed.xml">📡 Subscribe via RSS / Podcast app</a></p>
  <p class="meta">Add this feed URL to Spotify, Apple Podcasts, Pocket Casts, etc:<br>
  <code>{PAGES_URL}/feed.xml</code></p>
  <hr>
  {''.join(f"""
  <div class="badge">
    <div style="flex:1">
      <strong>{ep['title']}</strong><br>
      <audio controls src="audio/{ep['file']}"></audio>
    </div>
  </div>""" for ep in reversed(manifest['episodes']))}
</body>
</html>"""
    (DOCS_DIR / "index.html").write_text(html)


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    DOCS_DIR.mkdir(exist_ok=True)
    audio_dir = DOCS_DIR / "audio"
    audio_dir.mkdir(exist_ok=True)

    videos = get_video_list()
    total = len(videos)
    print(f"Channel has {total} videos total.")

    manifest = load_manifest()
    manifest["total_videos"] = total
    processed_ids = {ep["id"] for ep in manifest["episodes"]}

    batch = videos[START_INDEX: START_INDEX + BATCH_SIZE]
    print(f"Processing batch: videos {START_INDEX + 1}–{START_INDEX + len(batch)} of {total}\n")

    new_count = 0
    for i, video in enumerate(batch):
        vid_id = video["id"]
        title = video["title"]
        global_index = START_INDEX + i + 1
        filename = safe_filename(global_index, title)
        audio_path = audio_dir / filename

        print(f"[{global_index}/{total}] {title}")

        if vid_id in processed_ids or audio_path.exists():
            print("  Already done, skipping.\n")
            continue

        print("  Getting transcript...")
        transcript = get_transcript(vid_id)
        if not transcript:
            print("  No transcript — skipping.\n")
            continue

        print("  Summarising...")
        summary = summarize(title, transcript)

        print("  Generating audio...")
        asyncio.run(generate_audio(summary, audio_path))

        date_str = video.get("date", "20240101")
        try:
            dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
            pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            pub_date = ""

        manifest["episodes"].append({
            "id": vid_id, "title": title,
            "file": filename, "date": pub_date
        })
        new_count += 1
        print(f"  Done → {filename}\n")

    manifest["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_manifest(manifest)
    build_rss(manifest)
    build_index(manifest)

    print(f"\n✅ Batch complete. {new_count} new episodes. {len(manifest['episodes'])} total processed.")
    print(f"Podcast feed: {PAGES_URL}/feed.xml")


if __name__ == "__main__":
    main()
