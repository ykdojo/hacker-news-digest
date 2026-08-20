# System architecture

The diagram submitted to Devpost is [assets/architecture.png](assets/architecture.png), generated from [assets/architecture-diagram.html](assets/architecture-diagram.html) via `python3 assets/render_architecture.py`. Devpost requires the diagram as an uploaded file, not as a description, and it shares the dark cover style used for the article.

![System architecture](assets/architecture.png)

The per-node agent graphs (7 diagrams) are in the [article](https://medium.com/google-cloud/turning-hacker-news-into-a-daily-podcast-with-adk-2-gemini-tts-and-cloud-run-jobs-02c2d53fdcf2). This is the system-level picture the submission rules ask for.

## The same thing as text

Three different kinds of thing touch the job, and they are not peers. Cloud Scheduler is the control plane and only invokes. Hacker News is the data input and is only read. The Gemini models are dependencies the job calls out to. Everything else is downstream.

The models are reached two different ways. Every text agent runs on **gemini-3.7-flash through Vertex AI**, authenticated with the project's own credentials and billed to that project. The intro music also comes from Vertex AI. A `music_director` agent turns the day's headlines into a one-line music prompt, and **lyria-002** renders it as a short instrumental theme that gets faded under the hosts' opening lines. The **text-to-speech** step is the exception. It calls **gemini-3.1-flash-tts-preview through the Gemini API** with an API key, because the multi-speaker preview voices are served there.

A second, deliberately tiny **post-production job** (`hn-shownotes`, [shownotes/](shownotes/)) runs after the audio pipeline and never touches it. It is optional: the pipeline is complete without it, and the video edition is opt-in via `VIDEO=1` (off by default, since Veo is the dominant cost). It reads the published script, has **Gemma** (gemma-4-31b-it through the Gemini API) write the listener-facing episode description into the feed, maps each story's start time from the audio (Gemini audio understanding, snapped to the silences the pipeline inserts between TTS segments), has Gemma write a Veo prompt per story, renders an 8-second ambient backdrop per story with **Veo** (veo-3.1-fast through Vertex AI), and stitches them under the episode audio into a video edition. Any failure leaves the feed exactly as the pipeline published it.

Both jobs run on dedicated least-privilege service accounts: Vertex AI user, log writer, bucket-scoped storage, read access to the API-key secret, plus Cloud Trace agent on the pipeline job - and nothing else. The Gemini API key is mounted from Secret Manager rather than stored in plain env vars.

```mermaid
flowchart TB
  scheduler["Cloud Scheduler<br/>the only thing that starts a run"]
  hn["Hacker News<br/>Algolia API + article pages"]

  scheduler -.->|"invokes · daily 6:00 AM"| job
  hn -->|reads| job

  subgraph job["Cloud Run job: hn-digest. ADK 2 graph, ~11 min, ~$2-3"]
    direction TB
    fetch["Fetch the last 26 hours<br/>(code)"] --> curate["Curate 7-10 stories<br/>(model)"]
    curate --> digest["digest ×N, one lane per story<br/>(model)"]
    digest --> check["fact-check ×N<br/>(model)"]
    check -->|"repair ≤2"| digest
    check --> script["Write the script<br/>(model)"]
    script --> verify["Verify every claim<br/>(model)"]
    verify -->|"rewrite ≤2, then cut the segment"| script
    verify --> compose["Compose the intro theme<br/>(Lyria)"]
    compose --> tts["Render speech in segments<br/>(TTS)"]
    tts --> publish["Publish mp3 + RSS<br/>(code)"]
  end

  job -->|"text agents"| vertex["Vertex AI<br/>gemini-3.7-flash"]
  job -->|"intro music"| lyria["Vertex AI<br/>lyria-002"]
  job -->|"voice"| gapi["Gemini API<br/>gemini-3.1-flash-tts-preview, multi-speaker"]
  job -->|"writes the episode"| gcs[("Cloud Storage · public bucket<br/>mp3 · feed.xml · script · claim ledger · video edition")]
  gcs -->|"RSS 2.0"| apps["Any podcast app<br/>(follow by URL)"]

  scheduler -.->|"invokes · daily 6:30"| post["Post-production job: hn-shownotes<br/>Gemma shownotes · Gemini story timestamps ·<br/>Gemma Veo prompts · Veo backdrops · ffmpeg stitch"]
  post <-->|"reads the episode, writes shownotes + video"| gcs

  job -.->|"stdout · OTel spans"| obs["Cloud Logging · Cloud Trace"]
  obs -.->|"fetch_run.py / tail_run.py"| replay["Mission replay page<br/>(recorded + live)"]
```

## Design principles

Deterministic code owns the graph structure, fetching, scoring, the claim ledger, routing, TTS, the intro-music mix, and publishing. Models own only curation, digests, script-writing, claim extraction, fact-check verdicts, and the intro-music prompt. Every model output is schema-validated with Pydantic structured outputs.

Verification runs at two levels. Every story digest gets its own fact-check lane with up to 2 repair rounds, and the finished script goes through a claim-by-claim check with a rewrite loop before anything is rendered. A segment that still fails verification is cut rather than aired, which is why an episode can come in shorter than planned.

The job is stateless and run-to-completion. There are no servers between runs, so a failed run costs one execution rather than a standing service, and the idle system costs nothing.
