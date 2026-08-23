"""One command from Cloud Run to replay page: fetch a run's logs + ledger +
titles and write data.js.

    python3 fetch_run.py --date 2026-08-11
    python3 -m http.server 8749   # then open index.html

Credentials model: only `gcloud logging read` needs auth, and it uses your
existing local gcloud ADC. The ledger comes from the public GCS URL and titles
from the public HN API. Nothing secret ever reaches the page or the repo:
data.js contains only public episode data and pipeline progress lines.
"""

import argparse
import datetime as dt
import json
import subprocess
import sys

from parse_run import build, fetch_titles

PROJECT = "gemma-voice-agent-9107"
JOB = "hn-digest"
BUCKET = "hn-digest-9107"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="episode date, e.g. 2026-08-11")
    ap.add_argument("--project", default=PROJECT)
    ap.add_argument("--job", default=JOB)
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--out", default="data.js")
    args = ap.parse_args()

    day = dt.date.fromisoformat(args.date)
    lo, hi = f"{day}T00:00:00Z", f"{day + dt.timedelta(days=1)}T00:00:00Z"
    filt = (f'resource.type="cloud_run_job" AND resource.labels.job_name="{args.job}"'
            f' AND timestamp>="{lo}" AND timestamp<="{hi}"')
    lines = subprocess.check_output(
        ["gcloud", "logging", "read", filt, "--project", args.project,
         "--format", "value(timestamp,textPayload)", "--order", "asc",
         "--limit", "5000"], text=True).splitlines()
    if not lines:
        sys.exit(f"no logs for {args.date} (Cloud Logging keeps ~30 days)")

    url = f"https://storage.googleapis.com/{args.bucket}/episodes/{args.date}-ledger.json"
    raw = subprocess.run(["curl", "-sf", url], capture_output=True).stdout
    ledger = json.loads(raw) if raw else None
    if not ledger:
        print(f"warning: no public ledger at {url}; lanes will use story ids")

    titles = None
    if ledger:
        ids = list(dict.fromkeys(r["story_id"] for r in ledger))
        titles = fetch_titles(ids)

    data = build(lines, ledger, titles)
    with open(args.out, "w") as f:
        payload = json.dumps(data)
        f.write(payload if args.out.endswith(".json")
                else "const DATA = " + payload + ";\n")
    print(f"wrote {args.out}: {len(data['events'])} events, "
          f"{len(data['stories'])} stories, {len(data['claims'])} claims "
          f"(from {len(lines)} log lines)")


if __name__ == "__main__":
    main()
