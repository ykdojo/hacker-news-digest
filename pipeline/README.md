# HN daily podcast pipeline

The full pipeline behind [Turning Hacker News into a daily podcast with ADK 2, Gemini TTS, and Cloud Run jobs](https://ykdojo.github.io/awesome-agents-on-google-cloud/hn-daily-podcast): fetch the last 26 hours of Hacker News, curate, digest and fact-check per story, write and fact-check the script, render segmented TTS audio, and publish an mp3 plus RSS feed to a Cloud Storage bucket.

Ported from the public article repo for the All Things Agentic Hackathon submission, with two changes on top: the main model default is now `gemini-3.7-flash` (rules require Gemini 3.5+), and `ENABLE_TRACING=1` exports ADK's OpenTelemetry spans to Cloud Trace.

**The complete step-by-step spin-up guide (clone to running episode, plus the environment reference) is in the [main README](../README.md#run-it-yourself-step-by-step).**

## Environment

- `GEMINI_API_KEY`: key for the TTS calls (free tier works)
- `GOOGLE_GENAI_USE_ENTERPRISE=TRUE` plus application default credentials: for the text models, billed to your Cloud project
- `GOOGLE_CLOUD_LOCATION=global`
- `PUBLISH_BUCKET`: Cloud Storage bucket name. Unset writes to `./out` locally
- `STORY_CHECK=1`: per-story digest fact-checking. Recommended; it beat script-level-only checking in testing
- `DRY_RUN=1`: stop before TTS and publishing, for cheap logic tests
- `ENABLE_TRACING=1`: export agent/tool/model spans to Cloud Trace (service account needs `roles/cloudtrace.agent`, then view at Console > Trace explorer)
- `WINDOW_HOURS`: lookback window in hours, default 26
- `SEGMENT_WORDS`: target words per TTS segment, default 160 (about a minute of speech). Segments break only between speaker turns, and prefer to break where a new story starts
- `SEAM_GAP_MS`: silence inserted between TTS segments, default 350
- `INTRO_MUSIC=0`: disable the Lyria-generated intro theme, which is on by default. A `music_director` agent writes a music prompt from the day's headlines, Lyria renders a short instrumental clip, and ffmpeg fades it under the hosts' opening lines. Any Lyria failure just skips the music

## Files

- [pipeline.py](pipeline.py): the entire pipeline. The graph, the agents and their prompts, fact-checking, TTS rendering, and publishing.
- [Dockerfile](Dockerfile): container image for the Cloud Run job.
- [requirements.txt](requirements.txt): Python dependencies.
- [run_local.py](run_local.py): local runner that patches ADC with a gcloud token, for running outside Cloud Run.

## How the replay reads the pipeline

The pipeline's stdout is a deliberate data contract, not just debug noise. Every stage emits one structured line (`fact_check: 30 claims, 2 failed`, `review_router: 2 failed -> REWRITE #1`, one line per claim verdict with its text, `render_tts: ...`, `publish: ...`), `PYTHONUNBUFFERED=1` makes Cloud Logging receive them the moment they happen, and the [replay page's](../prototypes/replay/) parser turns those lines into node states, story lanes, claim chips, and stats. The same parser powers both modes: `tail_run.py` polls `gcloud logging read` and rewrites `live.json` for the live page, and `fetch_run.py` pulls the finished run's logs plus the published claim ledger for the full replay. So the visualization needs no hooks inside the pipeline and no credentials in the browser. Anything that can read the logs can drive it.
