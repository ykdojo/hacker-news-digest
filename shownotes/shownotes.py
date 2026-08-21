"""Companion job: Gemma shownotes + Veo video edition.

A second, deliberately tiny Cloud Run job that runs after the main pipeline
(scheduled 6:30 AM PT; the pipeline typically publishes by ~6:15). The main pipeline is
never touched; any failure here leaves the feed exactly as published.

Two steps, both driven by the day's published script:

1. Shownotes (Gemma): reads episodes/DATE-script.txt, writes a 2-3 sentence
   listener-facing episode description, and rewrites that episode's
   <description> in feed.xml.
2. Video edition (Veo, opt-in, OFF by default - set VIDEO=1): Gemini maps
   story timestamps from the audio and summarizes each ~10-second window,
   Gemma writes a mannequin-style scene per story plus a per-window action,
   Veo renders one 8s clip per window, and ffmpeg stitches them slowed
   under the audio into episodes/DATE-video.mp4.
   Skipped if the video already exists (idempotent). Veo dominates the cost;
   the shownotes step alone costs pennies.

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
SLOW = 2.0                       # slow-motion factor: 8s of footage covers 16s
COVER = 8.0 * SLOW               # hard ceiling per clip
WINDOW_TARGET = float(os.environ.get("WINDOW_TARGET", "10"))  # preferred window length


def story_windows(start, end):
    """Uniform windows of ~WINDOW_TARGET seconds (never over COVER, and never
    a sub-second remainder): the story's duration is split evenly."""
    import math
    dur = end - start
    n = max(1, round(dur / WINDOW_TARGET), math.ceil(dur / COVER))
    w = dur / n
    return [(start + k * w, start + (k + 1) * w) for k in range(n)]
DATE = os.environ.get("EPISODE_DATE") or datetime.datetime.now(
    ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")

MARKER = "Today's stories:"
ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")

NOTES_PROMPT = """You write podcast shownotes. Below is the full script of today's
episode of "Hacker News Digest", a two-host daily podcast that summarizes
Hacker News. Write the episode description: 2-3 sentences, plain text, no
markdown, no hashtags, no emoji. Lead with the main stories - the ones the
script spends the most time on - naming them specifically, then convey the
breadth of the quick stories. Do not invent anything that is not in the
script. Do not mention the hosts' names.

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

MANNEQUIN_STYLE = (
    "Photorealistic 3D render of living, animated mannequin people: completely smooth, "
    "blank, featureless heads with NO eyes, NO eyebrows, NO nose, NO mouth, NO lips, "
    "NO hair, NO ears, NO facial features of any kind - perfectly smooth blank ovals. "
    "Head surfaces are matte porcelain-white or light grey artificial material, "
    "absolutely not human skin. They wear varied everyday clothing that fits the scene "
    "- casual, workwear, or formal only when it suits. The mannequins are alive and move "
    "naturally like people, with lively, expressive body language: walking, turning, "
    "gesturing, handling objects as they act "
    "out the scene. Scenes feature zero, one, or two mannequins - a pure object or "
    "environment shot is fine - and never more than five in total. "
    "Soft cinematic studio lighting, shallow depth of field, muted "
    "color grade with one accent color, gentle camera movement, no text, no logos. "
    "Every single figure in the scene, foreground and background, is such a faceless "
    "living mannequin.")

INTRO_SCENE = ("Two mannequin figures seated at a news desk with studio microphones, "
               "one with a feminine silhouette in a tailored suit, one with a masculine "
               "silhouette in a suit, hands gesturing over the desk in a cozy news "
               "studio at sunrise. Each head is a single seamless smooth blank oval "
               "like a polished white egg: one perfectly uniform, featureless, "
               "unbroken surface from every angle. Accent color: warm amber.")

STORY_VISUALS_PROMPT = """Below is a numbered list of today's stories on a
technology news podcast. For EACH story write ONE line (under 40 words)
describing a scene grounded in that story's real subject, usually with zero,
one, or two faceless mannequin figures (at most five if truly needed): the
setting, what the mannequin is doing, key props, and one accent color. One
imaginative visual idea per scene is welcome - striking and cinematic, never
silly or absurd - and the story must stay instantly recognizable from the
image alone. Dress the mannequins in clothing appropriate to the setting
rather than defaulting to suits. No text, no logos, no brand names. Output
exactly {n} lines, in order, formatted as 'N. <line>' with nothing else.

STORIES:
{stories}
"""

WINDOW_NOTES_PROMPT = """This audio is a two-host news podcast episode. For each
time window below, write one short clause (under 15 words) saying what the
hosts are specifically discussing during that window. Output one line per
window, in order, formatted 'N. <clause>', nothing else.

WINDOWS:
{windows}
"""

BEATS_PROMPT = """A video backdrop shows one continuous scene behind a podcast
story.

Story: {story}
Setting: {theme}

The story's consecutive moments are:
{notes}

For EACH moment write ONE line (under 30 words): the same setting and the same
faceless mannequin figure(s) continuing the scene, with an action that clearly
depicts the specific thing being discussed at that moment. Keep strict visual continuity. No text, no logos.
Output exactly {n} lines, in order, formatted 'N. <line>' with nothing else.
"""

TIMESTAMPS_PROMPT = """This audio is a two-host news podcast episode. For each
story title below, give the timestamp where the hosts START discussing that
story. Output one line per story, in order, formatted exactly 'MM:SS | title',
nothing else.

STORIES:
{stories}
"""


def parse_numbered(text, n):
    """Parse 'N. <item>' lists; tolerates everything on one line by splitting
    on sequential inline numbers (so '6.1' inside an item never false-splits)."""
    lines = [re.sub(r"^\s*\d+\.\s*", "", l).strip()
             for l in text.splitlines() if re.match(r"\s*\d+\.\s", l)]
    if len(lines) == n:
        return lines
    parts = re.split(r"(?:^|(?<=\s))(\d+)\.\s+", text.strip())
    items = []
    for i in range(1, len(parts) - 1, 2):
        num, body = int(parts[i]), parts[i + 1].strip()
        if num == len(items) + 1:
            items.append(body)
        elif items:
            items[-1] = f"{items[-1]} {num}. {body}"
    return items if len(items) == n else lines


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
    cfg = types.GenerateVideosConfig(
        duration_seconds=8, aspect_ratio="16:9", number_of_videos=1,
        generate_audio=False,
        negative_prompt="face, eyes, eyebrows, nose, mouth, lips, teeth, hair, "
                        "ears, facial features, human skin, realistic person, "
                        "crowd, text, logos")
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


def assemble(tmp, clips, plan, boundaries, duration, audio, out):
    """Encode each sub-clip slowed 2x across its time range, fade between
    them, concatenate, and mux the episode audio."""
    from concurrent.futures import ThreadPoolExecutor
    ends = boundaries[1:] + [duration]
    jobs, idx = [], 0
    for seg, (start, end) in enumerate(zip(boundaries, ends)):
        for a, b in story_windows(start, end):
            jobs.append((idx, b - a)); idx += 1

    def encode(job):
        i, d = job
        cp, sp = f"{tmp}/clip{i}.mp4", f"{tmp}/seg{i}.mp4"
        open(cp, "wb").write(clips[i])
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", cp, "-t", f"{d:.3f}", "-an",
             "-vf", f"setpts={SLOW}*PTS,fps=24,scale=1280:720,format=yuv420p,"
                    f"fade=t=in:d=0.4,fade=t=out:st={max(d-0.4,0):.3f}:d=0.4",
             "-c:v", "libx264", "-b:v", "800k", "-preset", "fast", sp],
            check=True)
        return sp

    with ThreadPoolExecutor(max_workers=int(os.environ.get("ENCODE_WORKERS", "4"))) as pool:
        segs = list(pool.map(encode, jobs))
    concat_list = f"{tmp}/list.txt"
    open(concat_list, "w").write("".join(f"file '{sp}'\n" for sp in segs))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", concat_list, "-i", audio, "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", "-shortest", out], check=True)


def make_video(bucket, client, script, stories):
    import math
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
            # dedicated host-desk intro segment before the first story
            intro_end = min(12.0, max(boundaries[0], 4.0))
            boundaries = [0.0, intro_end] + boundaries[1:]
            text = gemma(client, STORY_VISUALS_PROMPT.format(
                n=len(stories), stories="\n".join(
                    f"{i+1}. {s}" for i, s in enumerate(stories))))
            themes = parse_numbered(text, len(stories))
            if len(themes) != len(stories):
                raise ValueError(f"{len(themes)} themes for {len(stories)} stories")
            themes = [INTRO_SCENE] + themes
        except Exception as e:  # degrade to one Gemma theme for the whole episode
            print(f"video: per-story path failed ({e}), single-theme fallback")
            boundaries = [0.0]
            themes = [gemma(client, VISUAL_PROMPT.format(script=script))[:500]]

        # Every second of video is unique footage: each time range is covered
        # by a sequence of distinct clips (slowed 2x), never a loop. Each clip's
        # action follows what is being said in its own time window.
        ends = boundaries[1:] + [duration]
        windows = []
        for seg, (start, end) in enumerate(zip(boundaries, ends)):
            for a, b in story_windows(start, end):
                windows.append((seg, a, b))
        beats = {}
        try:
            from google.genai import types as gtypes
            listing = "\n".join(f"{i+1}. {int(a)}s-{int(b)}s" for i, (_, a, b) in enumerate(windows))
            gem = genai.Client(vertexai=True,
                               project=os.environ["GOOGLE_CLOUD_PROJECT"],
                               location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
            notes_text = gem.models.generate_content(
                model=os.environ.get("TS_MODEL", "gemini-3.7-flash"),
                contents=[gtypes.Part.from_bytes(data=open(audio, "rb").read(),
                                                 mime_type="audio/mpeg"),
                          WINDOW_NOTES_PROMPT.format(windows=listing)]).text
            notes = parse_numbered(notes_text, len(windows))
            if len(notes) != len(windows):
                raise ValueError(f"{len(notes)} notes for {len(windows)} windows")
            for seg in range(len(themes)):
                seg_notes = [nt for (sg, _, _), nt in zip(windows, notes) if sg == seg]
                if len(seg_notes) <= 1 or seg == 0:
                    continue  # single-clip stories and the intro keep the plain scene
                txt = gemma(client, BEATS_PROMPT.format(
                    story=(["intro"] + stories)[seg], theme=themes[seg], n=len(seg_notes),
                    notes="\n".join(f"{i+1}. {nt}" for i, nt in enumerate(seg_notes))))
                bs = parse_numbered(txt, len(seg_notes))
                if len(bs) == len(seg_notes):
                    beats[seg] = bs
        except Exception as e:  # beats are an enhancement, never fatal
            print(f"video: window beats unavailable ({e}), using phase prompts")

        plan, prompts, seg_counter = [], [], {}
        for seg, start, end in windows:
            k = seg_counter.get(seg, 0)
            seg_counter[seg] = k + 1
            n = sum(1 for sg, _, _ in windows if sg == seg)
            theme = themes[seg]
            if seg in beats:
                prompts.append(f"{MANNEQUIN_STYLE} Setting: {theme} Now: {beats[seg][k]} "
                               f"Part {k+1} of {n} of one continuous take of the same scene.")
            else:
                prompts.append(f"{MANNEQUIN_STYLE} Scene: {theme} Phase {k+1} of {n} "
                               f"of one continuously evolving take of the same scene.")
            plan.append(seg)
        for t in themes:
            print(f"video: theme: {t[:100]}")
        print(f"video: {len(prompts)} unique clips planned")
        clips = veo_generate(prompts)
        assemble(tmp, clips, plan, boundaries, duration, audio, out)
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
