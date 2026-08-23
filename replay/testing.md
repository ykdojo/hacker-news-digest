# Testing the mission-replay stack

Everything here runs **without ever executing the pipeline**. The fixtures are
real artifacts from the Aug 11 2026 autonomous run: `fixtures/2026-08-11.log`
(actual Cloud Run log lines, including a real retry traceback as noise),
`2026-08-11-ledger.json` (the 50-claim fact-check ledger from GCS), and
`titles.json` (HN API titles). All commands run from `replay/`.

## Level 1: unit + integration (Python, no browser)

```
python3 -m unittest -v
```

11 tests in `test_parse_run.py`, ~0.02s. What they cover:

- **Full parse** (`TestParseFull`): 10 stories / 50 claims / 4 mains; the two
  fact-check passes come out as `pass 1 (40 claims, 1 fail)` then
  `pass 2 (50, 0)`; exactly one rewrite-edge event; every core node reaches
  `done` and the cutter never does; the final event carries the episode card
  with title, 8:35 duration, and 50/50; tracebacks and tenacity noise are
  dropped while the node-retry is surfaced as a meta line; end-state stats.
- **Incremental** (`TestIncremental`): every prefix of the log parses cleanly
  and event counts grow monotonically. This is exactly what `tail_run.py` does
  each poll cycle, so this test IS the live-mode parser contract. Also: with
  no ledger, lanes are derived from ids in the log (9 of 10, since the France
  story never needed a repair) — the degraded live-without-ledger path.
- **Cut path** (`TestCutPath`): synthetic lines exercise
  `rewrite cap hit -> CUT`, asserting cutter goes active then done on re-check.
  The real run never hit this branch, so only a synthetic test can cover it.
- **Golden** (`TestGolden`): parsing the fixtures must byte-for-byte equal the
  committed `data.js`. Regenerate after any parser change:

```
python3 parse_run.py --log fixtures/2026-08-11.log \
    --ledger 2026-08-11-ledger.json --titles titles.json --out data.js
```

## Level 2: browser self-test (one URL, no driver)

Serve the directory (`python3 -m http.server 8749`) and open:

```
http://localhost:8749/index.html?selftest
```

The page seeks through every event index 0..N (the same code path scrubbing
uses), then checks invariants: sweep completes without exception, publish node
done at the end, chips filled with zero failures remaining, episode card
visible, then seeks back to 0 and asserts a fully clean slate (empty terminal,
idle nodes, blank chips), then a mid-seek lands exactly. Result appears in the
console as `SELFTEST PASS` and programmatically as `window.__selftest`.

## Level 3: end-to-end via browser automation (what was actually run 2026-08-13)

Driven with the Claude-in-Chrome MCP; assertions via `javascript_tool`:

1. **Selftest**: loaded `?selftest`, read `window.__selftest` → `{pass: true,
   fails: []}`.
2. **Scrub state exactness**: `seek(iRewrite+1)` → asserted 39 ok chips + 1
   failed chip, router class `rewrite`, rewrite edge in `warnflow`, rewrite
   stat = 1.
3. **Real drag**: synthetic `left_click_drag` across the timeline → `idx` moved
   to 45/62, fill at 72.6%, tooltip showing `45/62`, graph state matching the
   digest phase. (This test caught a real bug: pointer-event capture didn't
   fire under synthetic input, so scrubbing is wired to mouse events.)
4. **Live mode**: simulator feeding the real log one line every 0.5s:

```
python3 tail_run.py --simulate fixtures/2026-08-11.log --sim-delay 0.5 \
    --ledger 2026-08-11-ledger.json --titles titles.json --out live.json
```

   Opened `index.html?live`, observed the LIVE badge and mid-run progression
   (fact-check node active while events streamed), then asserted the final
   state: 62/62 events applied, 50 ok chips, publish done, card visible with
   the real episode title, stats 50/50 and 8:35. (This caught a second bug:
   the auto-follow condition never fired after the initial lane rebuild.)

## Level 4: real Cloud Logging pull (verified 2026-08-13)

```
python3 fetch_run.py --date 2026-08-11 --out /tmp/e2e.js
```

Pulled the actual Aug 11 execution from Cloud Logging (1419 raw lines,
including 16 transient API errors that ADK's RetryConfig absorbed), the ledger
from its public GCS URL, and titles from the HN API. Output: 62 events,
10 stories, 50 claims — the retry noise collapses to one `node error ×16`
meta line and everything else matches the fixture build.

## Known gaps / notes

- `PYTHONUNBUFFERED=1` was set on the hn-digest job 2026-08-13 but no run has
  happened since, so live-tailing a real execution (and real per-line
  timestamps) is verified only via the simulator until the next run. The
  simulator exercises the identical parse/poll/apply path.
- `tail_run.py --project/--job` (gcloud polling) is unit-covered only via the
  shared parser; the gcloud subprocess path needs a real run to verify.
- Chip fail position within a pass is presentational (the log only says how
  many failed, not which claim), pinned to a fixed index for determinism.
