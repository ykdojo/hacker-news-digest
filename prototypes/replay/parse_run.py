"""Parse hn-digest run logs into replay events for the mission-replay page.

Sources:
  --log FILE|-        raw pipeline log lines (Cloud Logging textPayload or local
                      stdout; timestamps optional, noise lines are ignored)
  --ledger FILE       episode ledger JSON (claim list) -> chips tooltips + lane
                      order; optional (live runs have no ledger yet)
  --titles FILE       {story_id: title} JSON; optional
  --fetch-titles      fill missing titles from the HN Firebase API (via curl)

Output (--out): extension picks the format.
  data.js   -> "const DATA = {...};"  (static replay page)
  *.json    -> raw JSON               (live.json for ?live mode)

The same parse_lines() is used by tail_run.py for live mode, and by the unit
tests with fixtures/ so everything is testable without running the pipeline.
"""

import argparse
import json
import re
import subprocess
import sys

TS_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z?\s+")

PATTERNS = {
    "fetch": re.compile(r"fetch_candidates: (\d+) candidates"),
    "curate": re.compile(r"curate: (\d+) main \+ (\d+) quick"),
    "drop": re.compile(r"digest (\d+): dropped (\d+) unverifiable"),
    "repair": re.compile(r"story (\d+): (\d+) unsupported digest statement\(s\), repair round (\d+)"),
    "still": re.compile(r"story (\d+): still (\d+) unsupported after"),
    "digests_done": re.compile(r"digest_stories: (\d+) digests, (\d+) articles fetched"),
    "script": re.compile(r"write_script: (\d+) lines, (\d+) words"),
    "checked": re.compile(r"fact_check: (\d+) checked, (\d+) from cache"),
    "claim_line": re.compile(r"claim (\d+) \[(\w+)\] (ok|FAIL): (.*)"),
    "claims": re.compile(r"fact_check: (\d+) claims, (\d+) failed"),
    "rewrite": re.compile(r"review_router: (\d+) failed -> REWRITE #(\d+)"),
    "cut": re.compile(r"review_router: rewrite cap hit -> CUT #(\d+)"),
    "render": re.compile(r"review_router: all verified -> RENDER"),
    "compose_done": re.compile(r"compose_intro: (\d+) bytes|compose_intro: (skipped|no music)"),
    "tts_seg": re.compile(r"tts segment (\d+)/(\d+) \((\d+) words\)"),
    "tts_done": re.compile(r"render_tts: (\d+)s episode, (\d+) segments"),
    "publish": re.compile(r"publish: (\S+)"),
    "result": re.compile(r"RESULT: (.*)"),
    "node_err": re.compile(r"Node execution failed"),
}


def mmss(sec):
    return f"{sec // 60}:{sec % 60:02d}"


def parse_lines(lines, stories=None):
    """Returns (events, stories, claims). `stories` may grow if ids appear
    that are not in the provided list (live runs before the ledger exists).
    `claims` comes from per-claim log lines (newer pipelines log them), in
    ledger order for the latest completed pass; empty for older logs."""
    events = []
    stories = [dict(s) for s in (stories or [])]
    known = {s["id"]: s for s in stories}
    started = False
    pass_no = 0
    checked_log = None
    rewrites = 0
    verified = ""
    dur = ""
    cutter_active = False
    main_count = None
    claims_out = []
    pending_claims = []

    def ev(**kw):
        events.append(kw)

    def lane(sid):
        if sid not in known:
            s = {"id": sid, "title": f"story {sid}", "main": False}
            known[sid] = s
            stories.append(s)
        return sid

    def start():
        nonlocal started
        if not started:
            started = True
            ev(log="run start -> cloud run job hn-digest", meta=True,
               set={"fetch": "active"})

    for raw in lines:
        line = TS_PREFIX.sub("", raw.rstrip("\n"))
        text = line.strip()
        if not text:
            continue

        if m := PATTERNS["fetch"].search(text):
            start()
            ev(log=text, set={"fetch": "done", "curate": "active"},
               stat={"candidates": int(m[1])})
        elif m := PATTERNS["curate"].search(text):
            start()
            main_count = int(m[1])
            ev(log=text, set={"curate": "done", "digest": "active"},
               stat={"picks": int(m[1]) + int(m[2])}, lanes="show")
            for s in stories:
                ev(lane={"id": lane(s["id"]), "state": "active"},
                   log=f"  digesting {s['title'][:60]}", meta=True)
        elif m := PATTERNS["drop"].search(text):
            ev(log="  " + text, lane={"id": lane(int(m[1])), "badge": "drop"})
        elif m := PATTERNS["repair"].search(text):
            ev(log="  " + text,
               lane={"id": lane(int(m[1])), "state": "active",
                     "badge": f"r{m[3]}", "n": int(m[2])})
        elif m := PATTERNS["still"].search(text):
            ev(log="  " + text,
               lane={"id": lane(int(m[1])), "state": "done", "badge": "handoff"})
        elif PATTERNS["digests_done"].search(text):
            for s in stories:
                ev(lane={"id": s["id"], "state": "done"})
            ev(log=text, set={"digest": "done", "script": "active"})
        elif m := PATTERNS["script"].search(text):
            ev(log=text, set={"script": "done", "check": "active"},
               stat={"words": int(m[2])})
        elif m := PATTERNS["checked"].search(text):
            checked_log = text
        elif m := PATTERNS["claim_line"].search(text):
            pending_claims.append({"id": int(m[1]), "kind": m[2],
                                   "status": m[3], "claim": m[4]})
        elif m := PATTERNS["claims"].search(text):
            total, failed = int(m[1]), int(m[2])
            pass_no += 1
            if pending_claims:
                claims_out = pending_claims
                pending_claims = []
            extra = {}
            if cutter_active:
                extra = {"cutter": "done"}
                cutter_active = False
            ev(chips={"pass": pass_no, "count": total, "fail": failed},
               log=(checked_log or text) + " ...")
            checked_log = None
            verified = f"{total - failed}/{total}"
            ev(log=text, set={"check": "done", "router": "active", **extra},
               stat={"claims": verified,
                     **({"rewrites": 0} if pass_no == 1 else {})})
        elif m := PATTERNS["rewrite"].search(text):
            rewrites = int(m[2])
            ev(log=text, set={"router": "rewrite", "script": "active"},
               edge="rewrite", stat={"rewrites": rewrites})
        elif m := PATTERNS["cut"].search(text):
            cutter_active = True
            ev(log=text, set={"router": "rewrite", "cutter": "active"},
               stat={"rewrites": rewrites})
        elif PATTERNS["render"].search(text):
            ev(log=text, set={"router": "done", "lyria": "active"})
        elif PATTERNS["compose_done"].search(text):
            ev(log=text, set={"lyria": "done"})
        elif m := PATTERNS["tts_seg"].search(text):
            # lyria done here too: pre-music logs jump straight from RENDER to
            # the first tts segment and would otherwise leave the node active
            ev(log="  " + text, tts=int(m[1]),
               set={"lyria": "done", "tts": "active"})
        elif m := PATTERNS["tts_done"].search(text):
            dur = mmss(int(m[1]))
            ev(log=text, set={"tts": "done", "publish": "active"},
               stat={"episode": dur})
        elif m := PATTERNS["result"].search(text):
            tm = re.search(r"'title': '([^']*)'", m[1])
            ev(log="RESULT: episode live. "
               f"{verified or 'all'} claims verified. no human involved.",
               fin=True,
               card={"title": tm[1] if tm else "episode published",
                     "dur": dur, "verified": verified})
        elif PATTERNS["publish"].search(text) and text.startswith("publish:"):
            ev(log=text, set={"publish": "done"})
        elif PATTERNS["node_err"].search(text):
            if events and events[-1].get("meta") and "node error" in events[-1]["log"]:
                n = (re.search(r"×(\d+)", events[-1]["log"]) or [None, "1"])[1]
                events[-1]["log"] = (f"node error ×{int(n) + 1} "
                                     "(auto-retried by ADK RetryConfig)")
            else:
                ev(log="node error (auto-retried by ADK RetryConfig)", meta=True)
        # anything else (tracebacks, framework noise) is ignored

    if main_count is not None:
        for i, s in enumerate(stories):
            s["main"] = i < main_count
    return events, stories, claims_out


def stories_from_ledger(ledger, titles=None):
    titles = titles or {}
    seen, stories = set(), []
    for row in ledger:
        sid = row["story_id"]
        if sid not in seen:
            seen.add(sid)
            stories.append({"id": sid,
                            "title": titles.get(str(sid), f"story {sid}"),
                            "main": False})
    return stories


def claims_from_ledger(ledger):
    return [{"id": r["claim_id"], "story": r["story_id"], "kind": r["kind"],
             "claim": r["claim"], "note": r["note"][:180]} for r in ledger]


def fetch_titles(story_ids):
    titles = {}
    for sid in story_ids:
        out = subprocess.check_output(
            ["curl", "-s", f"https://hacker-news.firebaseio.com/v0/item/{sid}.json"])
        titles[str(sid)] = json.loads(out).get("title", f"story {sid}")
    return titles


def build(lines, ledger=None, titles=None):
    stories = stories_from_ledger(ledger, titles) if ledger else []
    events, stories, parsed_claims = parse_lines(lines, stories)
    return {"stories": stories,
            "claims": claims_from_ledger(ledger) if ledger else parsed_claims,
            "events": events}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="log file or - for stdin")
    ap.add_argument("--ledger")
    ap.add_argument("--titles")
    ap.add_argument("--fetch-titles", action="store_true")
    ap.add_argument("--out", default="data.js")
    args = ap.parse_args()

    lines = (sys.stdin if args.log == "-" else open(args.log)).readlines()
    ledger = json.load(open(args.ledger)) if args.ledger else None
    titles = json.load(open(args.titles)) if args.titles else None
    if ledger and args.fetch_titles:
        missing = [r["story_id"] for r in ledger
                   if not titles or str(r["story_id"]) not in titles]
        titles = {**(titles or {}), **fetch_titles(dict.fromkeys(missing))}
    data = build(lines, ledger, titles)
    payload = json.dumps(data)
    with open(args.out, "w") as f:
        f.write(payload if args.out.endswith(".json")
                else "const DATA = " + payload + ";\n")
    print(f"wrote {args.out}: {len(data['events'])} events, "
          f"{len(data['stories'])} stories, {len(data['claims'])} claims")


if __name__ == "__main__":
    main()
