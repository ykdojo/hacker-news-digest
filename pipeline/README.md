# HN daily podcast pipeline

The full pipeline behind [Turning Hacker News into a daily podcast with ADK 2, Gemini TTS, and Cloud Run jobs](https://ykdojo.github.io/awesome-agents-on-google-cloud/hn-daily-podcast): fetch the last 26 hours of Hacker News, curate, digest and fact-check per story, write and fact-check the script, render segmented TTS audio, and publish an mp3 plus RSS feed to a Cloud Storage bucket.

Ported from the public article repo for the All Things Agentic Hackathon submission, with two changes on top: the main model default is now `gemini-3.7-flash` (rules require Gemini 3.5+), and `ENABLE_TRACING=1` exports ADK's OpenTelemetry spans to Cloud Trace.

## Files

- [pipeline.py](pipeline.py): the entire pipeline. The graph, the agents and their prompts, fact-checking, TTS rendering, and publishing.
- [Dockerfile](Dockerfile): container image for the Cloud Run job.
- [requirements.txt](requirements.txt): Python dependencies.
- [run_local.py](run_local.py): local runner that patches ADC with a gcloud token, for running outside Cloud Run.

## Environment

- `GEMINI_API_KEY`: key for the TTS calls (free tier works)
- `GOOGLE_GENAI_USE_ENTERPRISE=TRUE` plus application default credentials: for the text models, billed to your Cloud project
- `GOOGLE_CLOUD_LOCATION=global`
- `PUBLISH_BUCKET`: Cloud Storage bucket name. Unset writes to `./out` locally
- `STORY_CHECK=1`: per-story digest fact-checking. Recommended, and the A/B winner
- `DRY_RUN=1`: stop before TTS and publishing, for cheap logic tests
- `ENABLE_TRACING=1`: export agent/tool/model spans to Cloud Trace (service account needs `roles/cloudtrace.agent`, then view at Console > Trace explorer)
- `MODEL_PRO` / `MODEL_FLASH`: override the model defaults (`gemini-3.7-flash` / `gemini-3.6-flash`)

## Run it yourself, step by step

The goal: from a fresh clone to a real episode running on Cloud Run. Every command is copy-pasteable after setting the four variables in step 2.

1. **Prereqs**: a Google Cloud project with billing, the `gcloud` CLI logged in (`gcloud auth login && gcloud auth application-default login`), Python 3.12+. Get a Gemini API key (free tier works) from https://aistudio.google.com/apikey for the TTS calls.
2. **Clone and set variables**:

   ```bash
   git clone https://github.com/ykdojo/hacker-news-digest.git
   cd hacker-news-digest/pipeline
   export PROJECT=your-project-id REGION=us-central1 BUCKET=your-bucket-name KEY=your-gemini-api-key
   gcloud config set project $PROJECT
   ```

3. **Enable APIs**:

   ```bash
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
     artifactregistry.googleapis.com storage.googleapis.com aiplatform.googleapis.com \
     cloudscheduler.googleapis.com telemetry.googleapis.com cloudtrace.googleapis.com
   ```

4. **Create the public bucket** (feed + episodes):

   ```bash
   gcloud storage buckets create gs://$BUCKET --location=$REGION
   gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
     --member=allUsers --role=roles/storage.objectViewer
   ```

5. **Cheap local test first**. No TTS and no publishing, so it stops before the paid steps. Edit `PROJECT` at the top of `run_local.py`, then:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   python3 -m pip install -r requirements.txt
   DRY_RUN=1 GEMINI_API_KEY=$KEY python run_local.py
   ```

   A virtualenv keeps this off your system Python. On many setups a bare `pip install` either fails with `externally-managed-environment` or `pip` is not on PATH at all. Tip: add `WINDOW_HOURS=3` to look at a shorter slice of Hacker News and cut the dry run's cost and time.

6. **Build and deploy the job**:

   ```bash
   gcloud artifacts repositories create pipeline --repository-format=docker --location=$REGION 2>/dev/null || true

   # Cloud Build needs a service account to run as. New projects no longer get the
   # legacy Cloud Build one, so grant the default compute account the build roles.
   export SA=$(gcloud iam service-accounts list --filter="email~compute@developer" --format="value(email)")
   for role in cloudbuild.builds.builder artifactregistry.writer storage.admin logging.logWriter; do
     gcloud projects add-iam-policy-binding $PROJECT \
       --member="serviceAccount:$SA" --role="roles/$role" --condition=None >/dev/null
   done

   gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT/pipeline/hn-digest \
     --service-account=projects/$PROJECT/serviceAccounts/$SA \
     --default-buckets-behavior=regional-user-owned-bucket
   gcloud run jobs create hn-digest --region $REGION \
     --image $REGION-docker.pkg.dev/$PROJECT/pipeline/hn-digest --task-timeout 3600 \
     --set-env-vars "GOOGLE_GENAI_USE_ENTERPRISE=TRUE,GOOGLE_CLOUD_LOCATION=global,GOOGLE_CLOUD_PROJECT=$PROJECT,PUBLISH_BUCKET=$BUCKET,STORY_CHECK=1,ENABLE_TRACING=1,PYTHONUNBUFFERED=1,GEMINI_API_KEY=$KEY"
   ```

7. **Run it** (~30 min, ~US$2-3 in model calls):

   ```bash
   gcloud run jobs execute hn-digest --region $REGION
   ```

   Watch progress in the Cloud Run console (execution logs), or live-tail it into the replay page (step 9).

   **On a brand new project, expect `429 RESOURCE_EXHAUSTED` from Vertex AI.** Fresh projects start with very little `gemini-3.7-flash` quota, and this pipeline fans out one digest call per story in parallel. ADK retries absorb some of it, but a cold project can still fail partway. Two ways around it: request Vertex AI quota for the model, or start smaller with `WINDOW_HOURS=4` so fewer stories qualify and fewer calls run at once.
8. **Subscribe**: when the run finishes, paste `https://storage.googleapis.com/$BUCKET/feed.xml` into any podcast app that follows shows by URL (Pocket Casts, Overcast, Apple Podcasts "Follow a Show by URL"). The episode mp3, script, and claim ledger are all in the bucket.
9. **Watch it as a mission replay** (optional, no extra credentials): see [prototypes/replay](../prototypes/replay/). `fetch_run.py --date YYYY-MM-DD` rebuilds any past run into an animated replay, and `tail_run.py` live-tails a running one.

   How this works: the pipeline's stdout is a deliberate data contract, not just debug noise. Every stage emits one structured line (`fact_check: 30 claims, 2 failed`, `review_router: 2 failed -> REWRITE #1`, one line per claim verdict with its text, `render_tts: ...`, `publish: ...`), `PYTHONUNBUFFERED=1` makes Cloud Logging receive them the moment they happen, and the replay's parser turns those lines into node states, story lanes, claim chips, and stats. The same parser powers both modes: `tail_run.py` polls `gcloud logging read` and rewrites `live.json` for the live page, and `fetch_run.py` pulls the finished run's logs plus the published claim ledger for the full replay. So the visualization needs no hooks inside the pipeline and no credentials in the browser. Anything that can read the logs can drive it.

   Live, mid-run. This is a real 2026-08-14 execution where the fact-check found 2 bad claims and fired the rewrite loop, all visible as it happened:

   ![Live rewrite loop](../prototypes/replay/media/shot3-live-rewrite-loop.jpg)
10. **Traces** (optional): with `ENABLE_TRACING=1` set (step 6 sets it), every agent/tool/model step shows up in Cloud Trace: Console > Trace explorer.
11. **Make it daily** (optional): the scheduler command below.

## Schedule it daily (step 11)

Uses the same variables as step 2. `SERVICE_ACCOUNT` is any service account with permission to run the job. The project's compute default works:

```bash
export SERVICE_ACCOUNT=$(gcloud iam service-accounts list --filter="email~compute@developer" --format="value(email)")
gcloud scheduler jobs create http hn-digest-morning \
  --schedule="0 6 * * *" --time-zone="America/Los_Angeles" \
  --uri="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/hn-digest:run" \
  --oauth-service-account-email=$SERVICE_ACCOUNT
```
