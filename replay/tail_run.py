"""Live tailer for the mission-replay page (?live mode).

Feeds live.json for index.html?live by re-parsing all lines seen so far on
each cycle. Two sources:

  Real run (requires PYTHONUNBUFFERED=1 on the job, else logs flush at the end):
    python3 tail_run.py --project gemma-voice-agent-9107 --job hn-digest \
        --ledger 2026-08-11-ledger.json --titles titles.json

  Simulation (no pipeline run, used for demos and e2e tests):
    python3 tail_run.py --simulate fixtures/2026-08-11.log --sim-delay 0.4 \
        --ledger 2026-08-11-ledger.json --titles titles.json

Writes are atomic (tmp + rename) so the page never reads a torn file.
"""

import argparse
import json
import os
import subprocess
import time

from parse_run import build


def write_out(path, data):
    tmp = path + ".tmp"
    data = dict(data, updated=time.time())
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def gcloud_lines(project, job, since):
    """Read the FULL window since `since` every call. Cloud Logging ingestion
    is not ordered, so any cursor scheme drops entries that become readable
    after the cursor has passed their timestamp (seen live 2026-08-14: all 26
    claim lines of a pass lost). Re-reading the window each poll self-heals;
    the parser re-parses everything anyway."""
    filt = (f'resource.type="cloud_run_job" AND resource.labels.job_name="{job}"'
            f' AND timestamp>"{since}"')
    out = subprocess.check_output(
        ["gcloud", "logging", "read", filt, "--project", project,
         "--format", "value(timestamp,textPayload)", "--order", "asc",
         "--limit", "5000"], text=True)
    return [r.split("\t", 1)[-1] for r in out.splitlines() if r.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", help="replay a log file instead of gcloud")
    ap.add_argument("--sim-delay", type=float, default=0.4)
    ap.add_argument("--project")
    ap.add_argument("--job", default="hn-digest")
    ap.add_argument("--since", default=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--ledger")
    ap.add_argument("--titles")
    ap.add_argument("--out", default="live.json")
    args = ap.parse_args()

    ledger = json.load(open(args.ledger)) if args.ledger else None
    titles = json.load(open(args.titles)) if args.titles else None
    lines = []

    if args.simulate:
        src = open(args.simulate).readlines()
        write_out(args.out, build([], ledger, titles) if ledger else
                  {"stories": [], "claims": [], "events": []})
        for line in src:
            lines.append(line)
            write_out(args.out, build(lines, ledger, titles))
            time.sleep(args.sim_delay)
        print(f"simulation done: {len(lines)} lines -> {args.out}")
        return

    if not args.project:
        ap.error("--project required unless --simulate")
    print(f"tailing {args.job} in {args.project} since {args.since} -> {args.out}")
    data = {"stories": [], "claims": [], "events": []}
    while True:
        try:
            fresh = gcloud_lines(args.project, args.job, args.since)
            if len(fresh) != len(lines):
                lines = fresh
                data = build(lines, ledger, titles)
                print(f"window now {len(lines)} lines")
            # heartbeat even with no new lines, so the page can tell a
            # running tailer from a leftover live.json
            write_out(args.out, data)
        except subprocess.CalledProcessError as e:
            print(f"gcloud error (retrying): {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
