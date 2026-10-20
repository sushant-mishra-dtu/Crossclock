# ADR 0003: Research brief and source drawer (C05)

Status: **Accepted** by the owner as proposed on 19 October 2026
(Asia/Calcutta), before any code was written, as C03's design was
([ADR 0001](0001-bounded-execution-and-run-status.md)): statement links in code
only, no brief fields the run does not produce, source texts kept locally and out
of downloads, and currency "not recorded" unless a source states it. Task: C05 in the
[plan](../plan.md) and [progress tracker](../progress.md). It builds on C04's
evidence records ([ADR 0002](0002-shared-evidence-and-bounded-retrieval.md)).
**Implemented** on 19-20 October 2026 and fixture tested, with a keyboard and
mobile walkthrough recorded; see [Implementation](#implementation-20-october-2026).
Evidence class for the proposal: source reviewed; for the implementation:
fixture tested. No provider or data vendor was called.

The plan's acceptance for C05: a user can inspect sources, dates, units and gaps
for an existing report; partial and unchecked states stay visible; a keyboard
and mobile walkthrough is recorded.

## Context

| Finding | Evidence |
| --- | --- |
| **What an analyst read is thrown away.** Each analyst runs in its own subgraph and returns only its report; its tool calls and their results stay in that private message history and are discarded. Nothing saved with a report says which data an analyst saw, from which vendor, for which window, or when it was fetched | [Graph setup](../../tradingagents/graph/setup.py) `_analyst_graph` |
| The Sentiment Analyst's news, StockTwits and Reddit feeds are fetched before its model runs, not through tools. Its saved Jev judgments (`sentiment_judgments.json`) keep each judged item's title, text, display date and author, and Jev's material-event probability, but no URL | [Sentiment analyst](../../tradingagents/agents/analysts/sentiment_analyst.py), [sentiment judgments](../../tradingagents/agents/sentiment_judgments.py) |
| C04's `EvidenceRecord` has the fields a drawer needs (source, operation, request and content hashes, fetch time, window, as-of, coverage; URL, publication time, period, currency and unit stay unknown). It covers only the router methods behind 6 of the 12 analyst tools, keeps only results that may be reused (an unavailable answer, the gap a drawer must show, gets no record), and lives in memory for the run's length only | [Evidence session](../../tradingagents/dataflows/evidence.py), [router](../../tradingagents/dataflows/router.py) |
| News feed items now carry the canonical URL and the full UTC publication time, unknown staying `None` (C04) | [Feed](../../tradingagents/dataflows/feed.py) |
| The claim check is saved only as Markdown appended to the decision. Its structured results (each claim's verdict and probability, the analyst-report section behind it, the figures no report states) are not saved. It checks the Portfolio Manager's thesis against the **analyst reports**, not against the original sources | [Claim check](../../tradingagents/agents/claim_check.py) `render_claim_check` |
| The Reports page shows the rating, section chips (I-V) and each section's Markdown. It drops the report header, so the run-status line, the models and the data vendors are not shown; it never reads `run_status.json`. A "complete with gaps" report (claim check not run) looks like a checked one. Partial reports are labelled | [Server](../../cli/webui/server.py) `report_detail`, [jobs](../../cli/webui/jobs.py) `load_report_sections`, [app.js](../../cli/webui/static/app.js) `/reports` |
| The live Analyze page shows the run's completion record, but runs are kept in memory only; after a restart the saved report is all there is | [Server](../../cli/webui/server.py) `_completion_view` |
| The structured outputs hold: Portfolio Manager rating, executive summary, investment thesis, price target, time horizon; Research Manager recommendation, rationale, strategic actions; Trader action, reasoning, entry, stop-loss, sizing. There is no structured business overview, list of risks or review trigger | [Schemas](../../tradingagents/agents/schemas.py) |
| The resolved instrument context gives company, exchange and classification. No currency is resolved for a run, and none is stated next to the price target | [Context](../../tradingagents/agents/context.py) |

## Proposal

### 1. Record what each analyst read

Every result an analyst receives, from a tool call or a prefetched feed, becomes
an evidence entry:

| Field | Value |
| --- | --- |
| `id` | `E1`, `E2`, ... in the order received; stable inside the report |
| `analyst`, `tool`, `arguments` | Who asked, which tool, the arguments after the trade-date clamp |
| `source`, `operation` | The vendor that answered and its method |
| `fetched_at` | UTC; shown in UTC and the browser's local time |
| `window_start`, `window_end`, `as_of` | From the request, as C04 records them |
| `coverage` | `available`, `empty`, `unavailable`, `no_data` or `withheld`, read from the router's sentinels and the feed's flag |
| `request_sha256`, `content_sha256` | As in C04; an entry served from the run's evidence session names the analyst it was shared with |
| `items` (feeds only) | Title, publisher, URL, `published_at` (unknown stays unknown, never the fetch time) |

Unavailable and no-data answers are recorded too: they are the "missing inputs"
the brief and drawer list. Entries travel as one more analyst output beside
`run_checks` (the existing `extra_outputs` mechanism), so a resumed run keeps the
evidence of the stages it reused.

The rendered results themselves go to a content-addressed folder,
`<results_dir>/evidence/<content_sha256>.txt`, written once and shared by later
runs that fetch the same bytes. Graph state and checkpoints carry only the
entries, not the text. Each report tree gains `evidence.json` (the entries).

### 2. Save the claim check as data

`claim_check.json` beside the report: the check's status (ran, failed, not set
up, turned off), the policy thresholds, and each claim with its verdict,
probability, the analyst-report section behind it (label and text) and the
figures no report states. What the decision's Markdown says does not change.

### 3. Link statements to sources in code

The thesis claims the claim check already splits out are the brief's
"statements". For each one, the brief shows, with no new model call:

- its claim-check verdict and the analyst-report section behind it (as today,
  now inspectable);
- **"Sources behind that section"**: the evidence entries that section's analyst
  read;
- **a figure trace**: each number in the claim, matched in code against the
  stored source texts at the claim's precision (the claim check's own matcher),
  giving "found in E4: Yahoo prices, 2026-09-15 to 2026-10-15, fetched 10:02
  UTC" or "in no source this run fetched".

The labels must say what was checked. "The analyst read these sources" is not
"this source supports the claim". Whether an original source supports or
contradicts a claim stays **not checked** in C05, and the drawer says so.

### 4. The brief, assembled from what the run saved

A **Brief** view opens each saved report, before sections I-V. Every field below
already exists in the saved state, `run_status.json` or the new files:

| Brief block | Content |
| --- | --- |
| Header | Ticker, company and exchange; analysis date (data as of); saved time with its time zone; run state in words (complete, complete with gaps and which check is missing, or partial and why it stopped); the rating, "no rating: partial research" for a partial run, and the rating after the claim check when it differs |
| Summary | The Portfolio Manager's executive summary, time horizon and price target. Currency is shown only when a source states it, otherwise "currency not recorded" |
| Statements | Each checked claim with its verdict, sources and figure trace (section 3), contradicted first, then not found, unverified and supported. Claims the check did not judge are listed as unchecked, never as supported |
| Headlines the analysts read | News items from the evidence entries, newest first, with publisher, publication time (or "unknown") and a link. Ordered by Jev's material-event probability when the sentiment judgments ran, and labelled so; "headlines read", not "material events" |
| Data coverage | One row per analyst: its sources, windows, fetch times and coverage, gaps marked in words |
| Missing inputs | Each gap with the fix where one exists: "Claim check not available: Jev is not set up (`TYPESAFE_API_KEY`)", "Fundamentals came from Yahoo: the India database has no filings for this company (`india import`)" |

A report saved before C05 has no `evidence.json`; its brief says sources were
not recorded for it and shows what its header and `run_status.json` hold.

### 5. The source drawer

- Opens from a statement, an evidence ID, or a "Sources (n)" button on each
  analyst section in I. A side panel at desktop widths, a full-screen sheet below
  640 px.
- Shows, per entry: source and tool, the analyst(s) who read it, window and
  as-of, fetch time, coverage, content hash, each feed item's original link and
  publication time, the passage around a matched figure, and "full text" loaded
  on demand.
- Verdicts are words, never colour alone: supported, contradicted, unverified,
  not found (against the analyst reports), and separately "figure found in a
  source" or "figure in no source".
- Keyboard: every opener is a button; Esc closes; focus moves into the drawer
  and back to its opener. Labelled regions for screen readers. Checked in both
  themes and at 375 px.
- The full-text endpoint serves only content hashes listed in that report's
  `evidence.json`, so no other file can be read through it.

### 6. Run status on saved reports

`report_detail` reads `run_status.json` and the header lines it drops today, and
the Reports page shows the run state on every report, so a decision whose claims
were not checked says so after a restart too.

### 7. Downloads

The Markdown and HTML reports gain a "Sources" appendix: each evidence ID with
source, window, fetch time, coverage and URL. Source texts are not copied into
downloads (size, and whether vendor text may be passed on is unreviewed).

## How C05 would be tested

Tests first, each seen failing on the unchanged code:

- On C01's real-graph fixture (scripted model, four analysts): every tool call
  and prefetch gives one entry; an unavailable answer is a gap entry; a resumed
  run keeps the reused stages' entries; an unknown publication time stays
  unknown; a source shared through the evidence session is stored once.
- Figure trace: the right entry is found; a near miss at the claim's precision
  is refused; a figure in no source is reported as such.
- Brief: a partial run shows no rating and why it stopped; a complete-with-gaps
  run names the missing check; a pre-C05 report says sources were not recorded.
- API: the full-text endpoint refuses a hash outside the report's manifest and
  any path-like id.
- Planted bugs, as in C03 and C04, and the full offline gate.
- A browser walkthrough with the scratch launch configuration: keyboard only,
  375 px, both themes; screenshots kept under `.scratch`.

No prompt, model, graph edge or provider setting changes under this proposal,
so C01's configuration hash and the bounded-execution rules stay as they are.

## Not in C05

- Checking claims against the original sources, by Jev or by number (C09 and the
  quality reference set).
- Ranking events by materiality beyond Jev's existing judgment.
- What changed between reports (C08), and reuse of stored source text across
  runs (C04's scope question).
- Explanations of ratios inside the drawer: the reports are free text, so
  naming the ratio behind a figure reliably needs structured facts (C08/C09).
  The screener catalog's descriptions can be linked once facts are structured.
- A pre-run preflight on the Analyze page. The brief's missing-input fixes come
  first; a preflight fits C06, which shows coverage before a run starts.
- Hosting, sharing and a review of the data rights behind showing vendor text.

## Questions for the owner

1. **Statement-to-source links.** (A, recommended) In code only, as in section
   3: section provenance plus a figure trace; no prompt change. (B) The analysts
   cite evidence IDs inline (`[E4]`). B gives sentence-level links but changes the
   prompts, so it needs the post-C03 baseline and the quality reference set
   first, and every cited ID must be checked against the manifest.
2. **Brief fields the run does not produce** (business overview, key risks,
   next review trigger). (A, recommended) Leave them out of C05 and say so.
   (B) Add `key_risks` and `review_trigger` to the Portfolio Manager's structured
   output now: a little more output per run, and a prompt change with the same
   baseline consequence as 1B.
3. **Storing source texts.** (A, recommended) Keep each rendered result locally,
   content-addressed under the results folder, and out of downloads. (B) Keep
   only the passages around matched figures. (C) Keep hashes only; the drawer
   then shows provenance but no text.
4. **Currency.** (A, recommended) "Not recorded" unless a source states it.
   (B) Infer it from the exchange suffix (`.NS` and `.BO` as INR), labelled
   inferred.

## Implementation, 20 October 2026

Built test-first, offline only, in the order of the sections above. The pieces:

| Piece | Where |
| --- | --- |
| Evidence entries, the router's acquisition notes, the content-addressed text store, the `evidence` reducer | [provenance.py](../../tradingagents/dataflows/provenance.py), [router.py](../../tradingagents/dataflows/router.py), [evidence.py](../../tradingagents/dataflows/evidence.py) |
| Recorded tool copies and the analysts' `evidence` output; an empty `evidence` in each new run's state | [setup.py](../../tradingagents/graph/setup.py), [state.py](../../tradingagents/agents/state.py), [propagation.py](../../tradingagents/graph/propagation.py) |
| The Sentiment Analyst's three feeds, on both its paths | [sentiment_analyst.py](../../tradingagents/agents/analysts/sentiment_analyst.py) |
| The claim check as data (`checked_claims`, `claim_check_data`), returned by the Portfolio Manager | [claim_check.py](../../tradingagents/agents/claim_check.py), [portfolio_manager.py](../../tradingagents/agents/managers/portfolio_manager.py) |
| `evidence.json`, `claim_check.json`, the Sources appendix (Markdown and HTML), the run's own store passed by every writer | [reporting.py](../../tradingagents/reporting.py), [report_html.py](../../tradingagents/report_html.py), [trading_graph.py](../../tradingagents/graph/trading_graph.py), [jobs.py](../../cli/webui/jobs.py), [run.py](../../cli/run.py) |
| The brief and the verified source texts | [brief.py](../../tradingagents/brief.py), [context.py](../../tradingagents/agents/context.py) (`resolved_identity`) |
| `/api/report/brief`, `/api/report/source`, run status on report details | [server.py](../../cli/webui/server.py) |
| The Brief view, section I's per-analyst buttons and the drawer | [app.js](../../cli/webui/static/app.js), [app.css](../../cli/webui/static/app.css) |

Where the build differs from, or adds to, the proposal:

- Tool results reach the analyst's node as the `ToolMessage` artifact of a
  copied tool (`response_format="content_and_artifact"`); the model is offered
  the same names, descriptions and schemas and receives the same text. A tool
  call ToolNode refuses for invalid arguments reads no source and gets no entry;
  any other tool error ends the run as before, and a partial report then lists
  what the finished analysts read.
- Each acquisition keeps the vendor's outcome, so a vendor chain shows every
  vendor asked ("sec_edgar: unavailable; yfinance: unavailable"). An answer the
  run's evidence session served again keeps the time it was really fetched and
  its C04 `request_sha256`. A read that does not go through the router (the
  market snapshot built from cached prices) says no vendor was recorded.
- The report writers take the run's own store (`evidence_store`) rather than
  the config current at save time: a graph saved under another config named the
  wrong folder before this.
- The Sources appendix links only web addresses, inside `<...>` and
  percent-encoded, with brackets and bars in vendor titles escaped: a feed's
  `javascript:` address had become a live link in a download. The page links
  only web addresses too and shows anything else as text.
- The Reports page strips the appendix by its exact opening, so an analyst's
  own `## ` headings stay in section I.
- Missing inputs name the check or source and its fix. The India-specific fix
  in section 4's example ("the India database has no filings") is not shown:
  the run records no signal that would say so reliably.
- A historical run's identity carries today's name only ("today's name" in the
  brief) and no exchange or classification, as the instrument context already
  withholds them for past dates.

### Offline evidence

40 new tests, each seen failing on the code before its change:
`test_c05_sources.py` (25: entries per tool call and feed, pipeline order,
vendor, window, fetch time and request identity, stored texts and their hashes,
shared reads stored once, gaps, links and unknown publication times, resume, the
Jev path, unchanged tool schemas, coverage words, the claim check as data,
`evidence.json` and `claim_check.json`, the appendix and its link safety, the
section split, partial reports, the run's own store, states saved before C05),
`test_c05_brief.py` (10) and `test_c05_webui.py` (5). Two existing tests changed
with the contract: the Sentiment Analyst subgraph's pinned outputs gain its
`evidence`, and a stand-in graph for `save_reports` gains the config every graph
has.

Planted bugs, one at a time against the C05 tests: 14 of 14 caught (tool
results dropped, a reducer keeping one analyst, an unavailable answer read as
available, stored texts not hash-checked, a reused answer re-stamped, the Jev
path's news unrecorded, claim-check sections dropped, the writer ignoring the
run's store, any link scheme in downloads, the appendix left in section V, a
store outside the results folder, an unlisted text served, figures matched on
digits alone, headlines oldest first).

Walkthrough ([record](../benchmarks/c05-walkthrough.json)): headless Chrome
driven by Playwright with the keyboard only, at 1440x900 in both themes and at
375x812, on three synthetic reports from one offline graph run (complete with a
throttled balance sheet and a hostile `javascript:` headline; partial; saved
before C05). 31 of 31 checks pass with no console errors: the brief opens by
default; statements in verdict order; links web-only and in a new tab without an
opener; times local with the UTC instant on hover; Tab reaches a statement's
sources, Enter opens the modal drawer with focus on its heading, focus stays in
it, Escape closes it and focus returns; the figure trace shows the stating line;
section I's buttons open each analyst's sources; partial and pre-C05 reports say
so; at 375 px nothing scrolls sideways and the drawer is full screen. It found
three faults, all fixed: disabling the "Show the text" button dropped keyboard
focus, the general dialog rule shrank the drawer (339 px on a phone), and the
coverage table hid its coverage and fetch-time columns.
[Drawer at 1440 px](../benchmarks/c05-drawer-1440.png),
[at 375 px](../benchmarks/c05-drawer-375.png). A planted removal of the drawer's
own focus return went unnoticed in Chrome, which restores focus on close itself;
the listener covers other browsers. A planted `javascript:` link was caught.

Full offline gate on tree `19891d8`: 2,265 passed, 4 skipped, 1 deselected,
0 failed; Ruff clean; scratch `HOME` left empty.

### Limits

- Fixture tested only. No live run, so no real vendor's texts, sizes or
  timings; how often a figure trace finds a coincidental equal number in real
  sources (a price row stating "109.50") is unmeasured. The drawer shows the line
  so the reader judges it.
- Texts are written for every graph run, backtests included, and nothing deletes
  them when a report is deleted. Identical texts are stored once.
- No user has used the brief (G4); its blocks are the design's, not observed needs.
