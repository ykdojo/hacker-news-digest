# Post-production job

Optional second Cloud Run job ([shownotes.py](shownotes.py)): Gemma writes each
episode's description into the RSS feed; with `VIDEO=1` it also produces the
video edition (Gemini maps story timestamps from the audio, Gemma writes a Veo
prompt per story, Veo renders 8-second backdrops, ffmpeg stitches them
under the audio). Any failure leaves the feed exactly
as the pipeline published it. See the [main README](../README.md) for what it
does and the [architecture notes](../architecture.md) for how it fits.

## Deploy (optional, after the pipeline works)

Uses the same variables as the main README's step 2. Least privilege: the job
gets its own service account with only Vertex AI, bucket access, logs, and the
API-key secret.

```bash
# service account with minimum permissions
gcloud iam service-accounts create hn-shownotes-job
export JOB_SA=hn-shownotes-job@$PROJECT.iam.gserviceaccount.com
for role in aiplatform.user logging.logWriter; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$JOB_SA" --role="roles/$role" --condition=None >/dev/null
done
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member="serviceAccount:$JOB_SA" --role=roles/storage.objectAdmin

# the Gemini API key lives in Secret Manager, not env vars
gcloud services enable secretmanager.googleapis.com
printf '%s' "$KEY" | gcloud secrets create gemini-api-key --data-file=-
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:$JOB_SA" --role=roles/secretmanager.secretAccessor

# build and create the job (video edition on; drop VIDEO=1 for shownotes only)
gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT/pipeline/hn-shownotes \
  --service-account=projects/$PROJECT/serviceAccounts/$SA \
  --default-buckets-behavior=regional-user-owned-bucket
gcloud run jobs create hn-shownotes --region $REGION \
  --image $REGION-docker.pkg.dev/$PROJECT/pipeline/hn-shownotes \
  --service-account=$JOB_SA --task-timeout 1800 --memory 4Gi --cpu 2 \
  --set-env-vars "PUBLISH_BUCKET=$BUCKET,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=global,PYTHONUNBUFFERED=1,VIDEO=1" \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest"

# run it for today's episode (the pipeline must have published first)
gcloud run jobs execute hn-shownotes --region $REGION

# optional: schedule it daily at 6:30 AM PT, after the 6:00 pipeline
export SERVICE_ACCOUNT=$(gcloud iam service-accounts list --filter="email~compute@developer" --format="value(email)")
gcloud scheduler jobs create http hn-shownotes-morning \
  --schedule="30 6 * * *" --time-zone="America/Los_Angeles" \
  --uri="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/hn-shownotes:run" \
  --oauth-service-account-email=$SERVICE_ACCOUNT
```

Local dry run (shownotes only, no video, costs pennies):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
GEMINI_API_KEY=$KEY PUBLISH_BUCKET=$BUCKET GOOGLE_CLOUD_PROJECT=$PROJECT \
  EPISODE_DATE=YYYY-MM-DD python shownotes.py
```
