"""Companion job: Gemma shownotes + Veo video edition.

A second, deliberately tiny Cloud Run job that runs after the main pipeline
(scheduled 6:45 AM PT; the pipeline publishes by ~6:30). The main pipeline is
never touched; any failure here leaves the feed exactly as published.

Two steps, both driven by the day's published script:

1. Shownotes (Gemma): reads episodes/DATE-script.txt, writes a 2-3 sentence
   listener-facing episode description, and rewrites that episode's
   <description> in feed.xml. Gemma also writes a one-line visual direction
   for step 2, the same way the pipeline's music_director writes Lyria's
   prompt.
2. Video edition (Veo, opt-in, OFF by default - set VIDEO=1): Veo renders
   ambient backdrops, ffmpeg assembles them under the episode audio, and the
   result is published as episodes/DATE-video.mp4. Skipped if the video
   already exists (idempotent). Veo dominates the job's cost; the shownotes
   step alone costs pennies.

Env:
  PUBLISH_BUCKET   bucket name (required)
  GEMINI_API_KEY   key for the Gemma call (required)
  GEMMA_MODEL      default gemma-4-31b-it
  VEO_MODEL        default veo-3.1-fast-generate-001 (via Vertex AI)
  GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION  for the Vertex Veo call
  VIDEO=1          enable the video edition (off by default; Veo cost)
  EPISODE_DATE     YYYY-MM-DD, default today in Pacific time
"""

import datetime
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from google import genai
from google.cloud import storage

BUCKET = os.environ["PUBLISH_BUCKET"]
GEMMA_MODEL = os.environ.get("GEMMA_MODEL", "gemma-4-31b-it")
VEO_MODEL = os.environ.get("VEO_MODEL", "veo-3.1-fast-generate-001")
VIDEO = os.environ.get("VIDEO", "0") == "1"
DATE = os.environ.get("EPISODE_DATE") or datetime.datetime.now(
    ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")

MARKER = "Today's stories:"
ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")

NOTES_PROMPT = """You write podcast shownotes. Below is the full script of today's
episode of "Hacker News Digest", a two-host daily podcast that summarizes
Hacker News. Write the episode description: 2-3 sentences, plain text, no
markdown, no hashtags, no emoji. Hook the listener with the most interesting
one or two stories, then convey the breadth of the rest. Do not invent
anything that is not in the script. Do not mention the hosts' names.

SCRIPT:
{script}
"""

VISUAL_PROMPT = """Below is the script of today's episode of a technology news
podcast. Write ONE line (under 60 words) describing an abstract, ambient,
seamlessly-looping background video that matches the day's overall mood:
slow continuous motion, cinematic, calm, no text, no logos, no people, no
readable objects. Mention colors and abstract forms only. Output only that
one line.

SCRIPT:
{script}
"""

STORY_VISUALS_PROMPT = """Below is a numbered list of today's stories on a
technology news podcast. For EACH story write ONE line (under 40 words)
describing an abstract ambient background video that evokes that story's
theme: slow continuous motion, cinematic, calm, no text, no logos, no people,
no readable objects, abstract colors and forms only. Output exactly {n}
lines, in order, formatted as 'N. <line>' with nothing else.

STORIES:
{stories}
"""

TIMESTAMPS_PROMPT = """This audio is a two-host news podcast episode. For each
story title below, give the timestamp where the hosts START discussing that
story. Output one line per story, in order, formatted exactly 'MM:SS | title',
nothing else.

STORIES:
{stories}
"""


def gemma(client, prompt):
    text = client.models.generate_content(model=GEMMA_MODEL, contents=prompt).text
    return re.sub(r"\s+", " ", text.strip())


def update_feed(bucket, summary):
    feed_blob = bucket.blob("feed.xml")
    root = ET.fromstring(feed_blob.download_as_text())
    for item in root.iter("item"):
        if item.findtext("guid") == f"hn-digest-{DATE}":
            desc = item.find("description")
            # Idempotent: keep only the pipeline's story list, then prepend.
            tail = desc.text[desc.text.index(MARKER):] if MARKER in desc.text else desc.text
            desc.text = f"{summary}\n\n{tail}"
            break
    else:
        raise SystemExit(f"shownotes: no feed item for {DATE}")
    feed_blob.upload_from_string(
        ET.tostring(root, encoding="unicode", xml_declaration=True),
        content_type="application/rss+xml")
    print(f"shownotes: feed.xml updated for {DATE}")


def vertex_client():
    return genai.Client(vertexai=True,
                        project=os.environ["GOOGLE_CLOUD_PROJECT"],
                        location=os.environ.get("GOOGLE_CLOUD_LOCATION_VEO", "us-central1"))


def veo_generate(prompts):
    """Render clips with a small concurrency window (Veo's long-running-request
    quota is per base model), backing off on 429s. Returns bytes per prompt."""
    from google.genai import types
    vertex = vertex_client()
    cfg = types.GenerateVideosConfig(duration_seconds=8, aspect_ratio="16:9",
                                     number_of_videos=1, generate_audio=False)
    limit = int(os.environ.get("VEO_CONCURRENCY", "2"))
    pending = list(enumerate(prompts))
    active, clips, throttles = {}, [None] * len(prompts), 0
    while pending or active:
        while pending and len(active) < limit:
            i, p = pending[0]
            try:
                active[i] = vertex.models.generate_videos(
                    model=VEO_MODEL, prompt=p, config=cfg)
                pending.pop(0)
            except Exception as e:
                if "RESOURCE_EXHAUSTED" not in str(e) and "429" not in str(e):
                    raise
                throttles += 1
                if throttles > 20:
                    raise
                print("video: veo start throttled, waiting 30s")
                time.sleep(30)
        time.sleep(10)
        for i in list(active):
            op = active[i] = vertex.operations.get(active[i])
            if op.done:
                clips[i] = op.result.generated_videos[0].video.video_bytes
                del active[i]
                print(f"video: clip {i + 1}/{len(prompts)} done")
    print(f"video: {VEO_MODEL} rendered {len(clips)} clips")
    return clips


def story_timestamps(audio_path, stories, duration):
    """Story start times: Gemini reads the audio, then each guess is snapped
    to the nearest inter-segment silence the pipeline inserted (SEAM_GAP_MS)."""
    from google.genai import types
    listing = "\n".join(f"{i+1}. {s}" for i, s in enumerate(stories))
    vertex = genai.Client(vertexai=True,
                          project=os.environ["GOOGLE_CLOUD_PROJECT"],
                          location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
    text = vertex.models.generate_content(
        model=os.environ.get("TS_MODEL", "gemini-3.7-flash"),
        contents=[types.Part.from_bytes(data=open(audio_path, "rb").read(),
                                        mime_type="audio/mpeg"),
                  TIMESTAMPS_PROMPT.format(stories=listing)]).text
    guesses = []
    for line in text.strip().splitlines():
        m = re.match(r"\s*(\d+):(\d\d)", line)
        if m:
            guesses.append(int(m.group(1)) * 60 + int(m.group(2)))
    if len(guesses) != len(stories) or guesses != sorted(guesses) or guesses[-1] >= duration:
        raise ValueError(f"implausible timestamps: {guesses}")

    probe = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", audio_path, "-af",
         "silencedetect=noise=-35dB:d=0.25", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", probe)]
    durs = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", probe)]
    seams = [s + d / 2 for s, d in zip(starts, durs)]
    snapped = [min(seams, key=lambda s: abs(s - g)) if seams and
               abs(min(seams, key=lambda s: abs(s - g)) - g) <= 3.0 else g
               for g in guesses]
    print(f"video: story starts {[round(s,1) for s in snapped]}")
    return snapped


def assemble(tmp, clips, boundaries, duration, audio, out):
    """Per story: loop its clip across its time range with fade in/out, then
    concatenate and mux the episode audio."""
    ends = boundaries[1:] + [duration]
    segs = []
    for i, (clip_bytes, start, end) in enumerate(zip(clips, boundaries, ends)):
        seg_dur = end - start
        clip, seg = f"{tmp}/clip{i}.mp4", f"{tmp}/seg{i}.mp4"
        open(clip, "wb").write(clip_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", clip,
             "-t", f"{seg_dur:.3f}", "-an",
             "-vf", f"scale=1280:720,fps=24,format=yuv420p,"
                    f"fade=t=in:d=0.5,fade=t=out:st={max(seg_dur-0.5,0):.3f}:d=0.5",
             "-c:v", "libx264", "-b:v", "800k", "-preset", "fast", seg],
            check=True)
        segs.append(seg)
    concat_list = f"{tmp}/list.txt"
    open(concat_list, "w").write("".join(f"file '{s}'\n" for s in segs))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", concat_list, "-i", audio, "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", "-shortest", out], check=True)


def make_video(bucket, client, script, stories):
    video_blob = bucket.blob(f"episodes/{DATE}-video.mp4")
    if video_blob.exists():
        print(f"video: episodes/{DATE}-video.mp4 already exists, skipping")
        return
    with tempfile.TemporaryDirectory() as tmp:
        audio, out = f"{tmp}/ep.mp3", f"{tmp}/out.mp4"
        bucket.blob(f"episodes/{DATE}.mp3").download_to_filename(audio)
        duration = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", audio], capture_output=True, text=True).stdout)

        try:
            boundaries = story_timestamps(audio, stories, duration)
            boundaries[0] = 0.0  # the first clip also covers the intro
            text = gemma(client, STORY_VISUALS_PROMPT.format(
                n=len(stories), stories="\n".join(
                    f"{i+1}. {s}" for i, s in enumerate(stories))))
            directions = [re.sub(r"^\s*\d+\.\s*", "", l).strip()
                          for l in text.splitlines() if re.match(r"\s*\d+\.\s", l)]
            if len(directions) != len(stories):
                raise ValueError(f"{len(directions)} directions for {len(stories)} stories")
            for d in directions:
                print(f"video: direction: {d[:100]}")
            clips = veo_generate(directions)
        except Exception as e:  # fall back to one ambient loop for the episode
            print(f"video: per-story path failed ({e}), falling back to single loop")
            direction = gemma(client, VISUAL_PROMPT.format(script=script))[:500]
            boundaries, clips = [0.0], veo_generate([direction])

        assemble(tmp, clips, boundaries, duration, audio, out)
        video_blob.upload_from_filename(out, content_type="video/mp4")
    print(f"video: published episodes/{DATE}-video.mp4")


def story_titles(bucket):
    feed = bucket.blob("feed.xml").download_as_text()
    root = ET.fromstring(feed)
    for item in root.iter("item"):
        if item.findtext("guid") == f"hn-digest-{DATE}":
            desc = item.findtext("description")
            if MARKER in desc:
                return [t.strip() for t in
                        desc.split(MARKER, 1)[1].split(";") if t.strip()]
    return []


def main():
    bucket = storage.Client().bucket(BUCKET)
    script_blob = bucket.blob(f"episodes/{DATE}-script.txt")
    if not script_blob.exists():
        print(f"shownotes: no script for {DATE}, nothing to do")
        return
    script = script_blob.download_as_text()
    stories = story_titles(bucket)
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    summary = gemma(client, NOTES_PROMPT.format(script=script))
    if not (40 <= len(summary) <= 800):
        raise SystemExit(f"shownotes: refusing summary of {len(summary)} chars")
    print(f"shownotes: {GEMMA_MODEL} wrote {len(summary)} chars for {DATE}")
    update_feed(bucket, summary)

    if VIDEO:
        try:
            make_video(bucket, client, script, stories)
        except Exception as e:  # the video edition is a bonus, never fatal
            print(f"video: skipped after error: {e}")


if __name__ == "__main__":
    sys.exit(main())
