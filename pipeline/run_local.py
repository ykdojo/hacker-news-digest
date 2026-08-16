"""Local runner: patches ADC with a gcloud token + quota project, then runs
the pipeline. Not needed on Cloud Run (service account provides ADC).

Usage: GEMINI_API_KEY=... python run_local.py
"""

import os
import subprocess

import google.auth
import google.oauth2.credentials

PROJECT = "gemma-voice-agent-9107"

token = subprocess.check_output(
    ["gcloud", "auth", "print-access-token"]).decode().strip()
creds = google.oauth2.credentials.Credentials(
    token=token, quota_project_id=PROJECT)
google.auth.default = lambda *a, **k: (creds, PROJECT)

os.environ.setdefault("GOOGLE_GENAI_USE_ENTERPRISE", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", PROJECT)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

import asyncio
import pipeline

asyncio.run(pipeline.main())
