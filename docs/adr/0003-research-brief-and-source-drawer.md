# ADR 0003: Research brief and source drawer (C05)

Status: **Proposed** on 19 October 2026 (Asia/Calcutta), for the owner to
approve or change before any code is written, as C03's design was
([ADR 0001](0001-bounded-execution-and-run-status.md)). Task: C05 in the
[plan](../plan.md) and [progress tracker](../progress.md). It builds on C04's
evidence records ([ADR 0002](0002-shared-evidence-and-bounded-retrieval.md)).
Evidence class: source reviewed. No provider or data vendor was called, and no
code has changed.

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
