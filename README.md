# Hacker News Digest

A daily podcast written, fact-checked, and published entirely by agents on Google Cloud.

Every morning at 6 AM Pacific, a Cloud Run job reads the last 26 hours of Hacker News, picks the stories worth talking about, digests and fact-checks each one against the actual linked articles and comment threads, writes a two-host script, verifies that script claim by claim, renders the audio with multi-speaker Gemini TTS, and publishes an mp3 plus an RSS feed to a public bucket. Paste the feed URL into any podcast app and it is a real show. No human is involved at any step.

- **Listen / demo page**: https://ykdojo.github.io/awesome-agents-on-google-cloud/hn-podcast-demo/
- **RSS feed** (works in any podcast app): https://storage.googleapis.com/hn-digest-9107/feed.xml
- **Write-up**: [Turning Hacker News into a daily podcast with ADK 2, Gemini TTS, and Cloud Run jobs](https://medium.com/google-cloud/turning-hacker-news-into-a-daily-podcast-with-adk-2-gemini-tts-and-cloud-run-jobs-02c2d53fdcf2) (Google Cloud - Community)

Built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/) (category: Taskmaster).

## Architecture

![System architecture](assets/architecture.png)

Deterministic code owns the graph structure, fetching, scoring, the claim ledger, routing, TTS, and publishing. Models own only what needs judgment: curation, digests, script-writing, claim extraction, and fact-check verdicts. Every model output is schema-validated with Pydantic structured outputs.

Verification runs at two levels: each story digest gets its own fact-check lane with up to 2 repair rounds, and the finished script goes through a claim-by-claim check with a rewrite loop. A segment that still fails verification is cut rather than aired, and every episode publishes its claim ledger next to the audio so any line of the show traces back to a verified claim.

Stack: **Google ADK 2** (graph workflow with dynamic per-story fan-out), **Gemini 3.7 Flash** via Vertex AI for all text agents, **multi-speaker Gemini TTS** for the two hosts, **Cloud Run jobs** + **Cloud Scheduler**, **Cloud Storage**, **Cloud Logging** + **Cloud Trace** (OpenTelemetry spans for every agent, tool, and model call). More detail in [architecture.md](architecture.md).

## Run it yourself

**[pipeline/README.md](pipeline/README.md)** is the complete clone-to-running-episode walkthrough: prerequisites, API enablement, a local dry run, deploying the Cloud Run job, and triggering a real episode. Every command is copy-pasteable.

The short version:

```bash
git clone https://github.com/ykdojo/hacker-news-digest.git
cd hacker-news-digest/pipeline
# then follow pipeline/README.md: set 4 variables, enable APIs,
# run locally with DRY_RUN=1, deploy as a Cloud Run job, execute
```

## Mission replay

[prototypes/replay/](prototypes/replay/) is an interactive replay of a real production run, built from the actual Cloud Run logs and the episode's claim ledger: the agent graph lights up stage by stage, story lanes show repair rounds, claim chips fill in as verification passes, and a rewrite loop fires on camera when a claim fails. It has a recorded mode (any past run via `fetch_run.py --date`) and a live mode that tails a run in progress. Tests and usage in [prototypes/replay/testing.md](prototypes/replay/testing.md).

```bash
cd prototypes/replay && python3 -m http.server 8000   # then open http://localhost:8000
```

## Numbers

- A clean run takes about **11 minutes** end to end and costs **~US$2-3 per episode** (one measured run: 659.2K tokens in / 117.7K out, visible in its Cloud Trace header). The idle system costs nothing.
- Stress-tested on day-one Gemini 3.7 Flash quota: dozens of 429s absorbed by ADK retries, 2 rewrite loops and both cut-segment fallbacks fired, and the episode still shipped fully verified with zero human intervention.
- Fact-check design was A/B tested: per-story checking (`STORY_CHECK=1`) beat script-level-only checking and is the production default.

## Repo map

| Path | What it is |
|---|---|
| [pipeline/](pipeline/) | the entire pipeline + step-by-step spin-up README |
| [prototypes/replay/](prototypes/replay/) | mission replay page (recorded + live) |
| [architecture.md](architecture.md) | system architecture, diagram + text |
| [assets/](assets/) | diagram + cover sources and render scripts |
