# Crossclock progress

Last updated: 19 October 2026 (Asia/Calcutta)
Execution plan: [plan.md](plan.md)  
Baseline reviewed: `4b2a3f781fdda6cb3a7594c3191122d4fe5b6836`

## Current position

The measurement sprint started on 14 October 2026. **C01 is complete as a diagnostic baseline** under the registered 180-second NVIDIA timeout: three matched cold/warm pairs on one configuration, with every warm run's cache seed verified by hash. On 16 October the owner approved a new 144-send NVIDIA allowance. A seeded warm run finished recovery pair 2 and pair 3 ran cold and warm, using 83 sends and 24 Jev sends with no failure. Median research time to a persisted report was 371.908 s cold and 435.775 s warm (n=3 each). The 10 ms synthetic source cache cannot explain that difference, so there is no cache-speed claim. The original 60-second cohort keeps one complete pair and is not pooled. [The benchmark record](benchmarks/c01.md) has the details. No live calls remain running.

**C03 now passes its full offline acceptance gate** (19 October; review/merge pending). Both failures left from 17 October were outdated fixtures: the sentiment output now includes `run_checks`, and the memory regression must supply completed required stages and use the real completion gate. Production completion checks remain intact. The Windows Python 3.13 gate passed 2,120 tests, with four skips (three POSIX-only checks and optional Bedrock), one integration test deselected, and Ruff clean. Its [design](adr/0001-bounded-execution-and-run-status.md) was approved as proposed: a 1,200 s Standard deadline, a 180 s attempt timeout for every provider, partial reports saved by default, and claim-check failures labelled only. Source inspection found the app had no request timeout at all for NVIDIA and Anthropic models; every provider now gets one. One retry loop with deadline-aware waits replaces the two stacked layers. Stop and the deadline refuse every later HTTP send, SDK and Jev retries included. A stopped, timed-out or failed run saves a partial report with no rating, and records no decision. Offline tests cover these paths; no live provider call or browser walkthrough was made.

**C04 is built and fixture tested** (19 October; review pending; [design record](adr/0002-shared-evidence-and-bounded-retrieval.md)). Each research run gets its own evidence session: identical requests in the run fetch once, concurrent callers share one in-flight request, failures and unavailable answers are never reused, price and news are reused for 60 s and financials for 300 s, and each source has at most two acquisitions in flight. Inside a run the news text and the news items share one acquisition. Alpha Vantage feed items are now trimmed to the requested window, and feed items keep their URL and full UTC publication time. On synthetic sources, a seven-read router workload fell from 42 to 18 acquisitions and C01's real-graph fixture from 13 to 12, with every routed answer unchanged. There is no latency, cost or live duplicate-rate claim: the fixtures have no delays and no live run was made. The code reached `main` as `07220d2`, committed and pushed from the shared checkout before the end-to-end wiring test and these documents were finished; a follow-up PR adds them.

**C05's design is accepted** (19 October; [ADR 0003](adr/0003-research-brief-and-source-drawer.md)): the owner approved all four recommended options the same day. Implementation has started. Source review found that what each analyst read is discarded with its private message history, the claim check is saved only as Markdown, and the Reports page shows neither the run status nor any source. The proposal records every tool result and prefetched feed as an evidence entry (gaps included), stores the texts content-addressed, saves the claim check as data, links thesis claims to sources in code (the section's sources plus a figure trace, no new model call), assembles a brief from saved fields, and adds a keyboard- and mobile-ready source drawer. Under the recommended options no prompt, model or graph edge changes.

Next task: **C05 implementation** per the accepted ADR 0003, test-first. The owner questions on C04's reuse scope and limits are in its design record. C03's new configuration keys give C01 benchmark-runner runs a new configuration hash, so later experiment comparisons need a post-C03 baseline (see open decisions). The selected shorter, source-grounded bull/bear experiment compares against the 180-second baseline after a held-out claim/coverage set exists. Run **C02, user-task and data-access discovery**, alongside them when the project owner is available.

The initial audience remains an NSE watchlist investor hypothesis, with US-equity validation. The owner authorized limited NVIDIA quota use and Jev up to US$1. Keys were checked without displaying them and both live services responded. No achieved product speedup, customer validation or real-source completion-time claim exists.

## Status definitions

| Status | Meaning |
| --- | --- |
| Not started | No implementation or experiment attempt recorded |
| In progress | Work attempted; acceptance evidence remains incomplete |
| Blocked | An attempted task has a concrete unresolved dependency and recovery action |
| Complete | Required acceptance evidence exists and is linked |
| Deferred | Intentionally scheduled after a prerequisite or decision |

Evidence labels are separate: **source reviewed**, **external source reviewed**, **fixture tested**, **live measured**, and **user observed**. A test file's existence does not mean its tests were run. No aggregate percentage combines planning, implementation and customer validation.

## Task tracker

| ID | Task | Status | Owner | Evidence / next action |
| --- | --- | --- | --- | --- |
| C00 | Repository research, plan and progress tracker | Complete | Codex | [Plan](plan.md), linked repository evidence and dated external sources; documentation only |
| C01 | Instrumentation and baseline | Complete | Codex; completed in Claude Code | [Benchmark record](benchmarks/c01.md): 180-second cohort has three matched pairs with verified cache seeds; 60-second cohort keeps one pair, not pooled. Across all six graph attempts: 9 completions, 5 failures, 11 unstarted. Diagnostic scope only: synthetic sources, no UI first-useful-result, quality or tail claim |
| C02 | User tasks and data-access inventory | Not started | Unassigned: product owner | Validate target cohort, prepare three pilot tasks, record access/rights and any actual user feedback |
| C03 | Deadlines, retries, cancel and partial states | In progress: offline gate passed; review/merge pending | Codex (regressions); Claude Code (implementation) | [Design and implementation](adr/0001-bounded-execution-and-run-status.md); [acceptance tests](../tests/test_c03_run_control.py) and full offline gate pass (2,120 passed, Ruff clean). Both regressions reproduced before fixture fixes; production unchanged. Not live measured: no provider call, no browser walkthrough, deadline-miss rate unknown |
| C04 | Shared evidence and bounded retrieval | In progress: fixture tested; review pending | Codex (implementation); Claude Code (graph-run isolation, measurement, review) | [Design record](adr/0002-shared-evidence-and-bounded-retrieval.md); 105 offline tests; [summary](benchmarks/c04-summary.json): 42 -> 18 acquisitions on the router workload, 13 -> 12 on C01's graph fixture, answers unchanged. Full offline gate 2,225 passed. Not live measured: no latency, cost or real-source duplicate rate |
| C05 | Brief and source drawer | In progress: design accepted, implementation started | Claude Code | [Design](adr/0003-research-brief-and-source-drawer.md), accepted as proposed. No code, tests or walkthrough yet |
| C06 | Research modes and first Quick path | Not started | Unassigned: engineering | Sprint stretch or next sprint; bounded Quick path distinct from a full decision; preserve Standard baseline |
| C07 | Ten-day checkpoint | Not started | Unassigned: project owner | Review baseline, measured improvement, quality and user-task evidence; choose the next P1 slice |
| C08 | Report versions and incremental research | Deferred | Unassigned | After C04-C07; compare facts/sources and invalidate dependent conclusions |
| C09 | Filing retrieval and Q&A | Deferred | Unassigned | After C04/C05; start with accessible filings and held-out source-grounding questions |
| C10 | Thesis notebook and digest | Deferred | Unassigned | After C08; extend existing alert deduplication, channels and watchlists |
| C11 | Research comparisons, portfolio impact and query drafts | Deferred | Unassigned | After C05/C08 and user-demand evidence; reuse peers, holdings and query compiler |
| C12 | Shared scheduler and throughput | Deferred | Unassigned | After C01/C03/C04 and measured queue need; include direct Analyze and batch work in limits |
| C13 | Versioned 129BOB research export | Deferred | Unassigned | After C05/C08 and confirmed integration need; verify importer/approval compatibility |
| C14 | Pilot and release decision | Deferred | Unassigned: project owner | After selected P1 work; review user value, quality, performance, cost and distribution prerequisites |

## Baseline findings

| Finding | Evidence class / source | What remains unverified |
| --- | --- | --- |
| Analysts run concurrently; research and risk stages follow sequentially | Source reviewed: [graph setup](../tradingagents/graph/setup.py) | Actual duration and critical path on the selected provider |
| Default research/risk limits are one round each; tool cap is 20 | Source reviewed: [defaults](../tradingagents/default_config.py) | Which calls or output lengths can be reduced without losing quality |
| Outer transient retry waits sum to 190 seconds when all are taken, in addition to SDK attempts. C03: the outer loop is now the only retry layer, its waits stop at the deadline and on Stop | Source reviewed: [OpenAI-compatible client](../tradingagents/llm_clients/openai_client.py); fixture tested ([C03 tests](../tests/test_c03_run_control.py)) | Frequency and real impact; this is not an observed average or maximum run time |
| Gemini has an explicit 600-second request timeout. OpenAI-compatible and Anthropic models had none: LangChain passed `timeout=None`, which those SDKs read as no timeout. C03 passes 180 s to every provider | Source reviewed: [Google client](../tradingagents/llm_clients/google_client.py); offline SDK inspection ([ADR 0001](adr/0001-bounded-execution-and-run-status.md)) | Whether 180 s and 1,200 s suit real sources and other providers; deadline-miss rate |
| Sentiment sources are fetched sequentially | Source reviewed: [sentiment analyst](../tradingagents/agents/analysts/sentiment_analyst.py) | Net benefit of bounded concurrent fetches and shared news |
| Memory settlement participates in the initial graph barrier | Source reviewed: [graph setup](../tradingagents/graph/setup.py), [memory step](../tradingagents/graph/trading_graph.py) | Its contribution to wait time and a safe frozen-snapshot alternative |
| Batch analysis queue is serial; direct analyses start separately | Source reviewed: [jobs](../cli/webui/jobs.py) | Throughput under actual quotas; behavior of a shared scheduler |
| Jev batch requests already use up to eight workers | Source reviewed: [Jev client wrapper](../tradingagents/agents/jev.py) | Aggregate concurrency, paid call counts and reuse opportunity |
| Cache, screening, peers, watchlists, holdings, exports, alerts and checkpoints exist | Source reviewed: [dataflows](../tradingagents/dataflows/), [screener](../tradingagents/screener/), [checkpointer](../tradingagents/graph/checkpointer.py) | Fresh runtime behavior; no regression suite rerun during planning |
| Final claim checks compare with analyst reports | Source reviewed: [claim checker](../tradingagents/agents/claim_check.py) | Original-source support and full material numerical verification |
| Historical social/news feeds have coverage limitations | Source reviewed: [sentiment analyst](../tradingagents/agents/analysts/sentiment_analyst.py) | Archived coverage and model knowledge leakage; date filtering alone is insufficient |
| Existing 129BOB importer reads upstream reports as LLM-authored evidence | Source reviewed: [sibling importer](../../129BOB/apps/api/bob_api/research/upstream.py) | Compatibility with a proposed structured export; no integration run made |

## Metrics ledger

Targets below are proposals from the plan. A blank baseline is unknown, never zero.

| Metric | Baseline | Candidate | Provisional target / next measurement |
| --- | --- | --- | --- |
| Warm first useful facts, p50 / p95 | Not measured | Not implemented | <=5 s / <=10 s for the proposed Quick workflow |
| Quick completed brief, p50 / p95 | No Quick path | Not implemented | <=30 s / <=60 s under the registered workload |
| Standard complete report, p50 / p95 | 180-second baseline, research setup to persisted report on synthetic sources: cold median 371.908 s (341.489-417.197 s), warm median 435.775 s (285.266-494.657 s), n=3 each; p95 unqualified. Original 60-second cohort: one matched pair, cold 189.327 s / warm 175.019 s, kept separate | Not measured | Compare candidates against the 180-second baseline under the same policy; do not pool timeout variants |
| Cold first visit and final report | Separately measured dependency startup: 79.012 s, 57.143 s and 174.749 s; browser first visit unknown | Not measured | Keep startup separate from research and measure UI rendering |
| Queue wait and 10-symbol completion | Not measured | Not measured | Identify whether provider limits or serial scheduling dominate |
| Report cost and failed-run spend | All attempts/probes: Jev reported estimate $0.00428350, conservative reservation $0.239468544; NVIDIA dollar cost unknown, 377 sends total; 229 NVIDIA / 55 Jev sends on 16 October | Not measured | Guard/trace counts reconcile; cumulative Jev remains below US$1; 192-send continuation used up, 83 of 144 used in the completion allowance; [budget record](benchmarks/c01.md) |
| Source-request count / cache eligibility | Without sharing, on synthetic sources: 42 acquisitions (router workload, 6 pairs) and 13 (C01 graph fixture) | Fixture tested (C04): 18 and 12, every answer unchanged; unavailable answers never reused ([summary](benchmarks/c04-summary.json)) | Live duplicate rate on real sources and the latency effect of the two-per-source cap; needs a live allowance |
| Supported or explicitly unsupported material claims | Not audited | Not audited | >=95% on initial frozen claim set; distinguish citation presence from correctness |
| Audited material numeric errors | Not audited | Not audited | Zero; any material mismatch blocks the release candidate |
| Material-event recall / claim accuracy | Not audited | Not audited | No more than five-percentage-point drop versus full workflow; assess uncertainty |
| Completion / partial / failure / deadline-miss rates | All six graph attempts: 9 completed, 5 failed (two local guards, three 60-second response timeouts), 11 unstarted; deadlines unqualified | Not measured. C03 labels each run (`complete`, `complete_with_gaps`, `partial`, `none`) with a stop reason, in its trace and `run_status.json` | Publish alongside latency; historical/repeated attempts are not operational rates |
| Human research-task time | No participants observed | No participants observed | Pilot median improvement >=30%; >=4/5 complete each task unaided |

Use at least three paired repetitions per instrument/cache regime for diagnosis, and at least 100 comparable runs per claimed mode/regime before reporting p95 as qualified. Live runs require a recorded spending cap. See [measurement rules](plan.md#measurement-and-quality-gates) for the full protocol.

## Open decisions and prerequisites

| Item | Current state | Resolution |
| --- | --- | --- |
| Main user segment | NSE investor hypothesis; US validation retained | C02 observes actual tasks and willingness to return/pay |
| First release setting | Local application assumed | Confirm whether local use meets pilot needs before designing hosted accounts |
| Provider/model and budget | Nemotron 3 Super and Jev 1.13 pinned; Jev total cap US$1; NVIDIA remaining quota unknown | [Registered limits and exact settings](benchmarks/c01.md#registered-workloads-and-budgets) |
| Current report time/cost | 180-second baseline: three matched pairs, about 6-7 minutes per synthetic-source report; 11 of 206 recovery responses took over 60 seconds (maximum 125.456 s); cumulative usage reconciled | [Recovery completion](benchmarks/c01.md#recovery-completion-16-october); no speedup or cache-benefit claim |
| Historical and transcript coverage | Incomplete/unverified | C02/C09 record source availability, knowledge times and reuse permissions |
| Data distribution rights | No deployment-specific entitlement review performed | Check actual source agreements before hosting or redistributing data |
| Named owners/start date | C01 owned by Codex; started 14 October 2026, completed 16 October 2026. C03 built in Claude Code on 17 October 2026. C04 built by Codex and completed in Claude Code on 19 October 2026. C05 design proposed in Claude Code on 19 October 2026 | Product/study owners remain open |
| Evidence reuse scope and limits | Per research run; 60 s price/news, 300 s financials; two acquisitions in flight per source (C04) | Owner: answer [ADR 0002's questions](adr/0002-shared-evidence-and-bounded-retrieval.md#questions-for-the-owner) |
| Brief and source drawer design | Accepted 19 October: code-only statement links, no new brief fields, source texts kept locally and out of downloads, currency "not recorded" unless stated (C05) | Resolved; see [ADR 0003](adr/0003-research-brief-and-source-drawer.md) |
| Baseline for later experiments | C03 adds three configuration keys, so C01 benchmark-runner runs now get a new configuration hash; the warm-seed check refuses C01 caches | Before the concise bull/bear experiment, register a post-C03 baseline (needs a live allowance) or compare against C01 with an explicit policy note |
| Runs without Jev | Without `TYPESAFE_API_KEY`, every complete run reads "complete with gaps": the claim check and Jev judgments are unavailable, as approved | Owner: keep, or treat an unconfigured Jev as switched off (not a gap) |
| Quality reference set | Not created | Freeze questions, claims, material events and held-out evaluation cases before optimization |
| Existing API keys/services | Both keys confirmed locally; six successful probes per provider | Secrets stay in ignored env files; trace metadata excludes credentials |

The open decisions above are prerequisites. C01 has no remaining dependency.
Any further live run needs its own recorded allowance: the 192-send
continuation is used up, and the 144-send completion allowance belonged to its
single attempt.

## Gate ledger

| Gate | Current verdict | Required evidence |
| --- | --- | --- |
| G1: reproducible baseline | Passed for the synthetic-source diagnostic baseline: three matched 180-second pairs, verified cache seeds, reconciled ledgers; the 60-second cohort stays separate | Real-source timing, UI first-useful-result and the NSE/US instrument matrix remain unmeasured; retain source-provenance limits, failures and coverage gaps |
| G2: data and execution correctness | Partly evaluated: deadline, retry, Stop, partial-state and resume cases pass offline (C03); freshness, as-of, duplicate-request and isolation cases pass offline (C04) | Live confirmation of deadline and Stop behavior, and of source freshness on real vendors |
| G3: useful speed improvement | Not evaluated | Matched latency/cost results plus coverage and quality checks |
| G4: user value | Not evaluated | Observed task completion/time and reasons users would return |
| G5: release or pilot readiness | Deferred | Relevant G1-G4 evidence, usable data access and a chosen release scope |

Planning completion passes none of the product gates.

## Activity log

| Date | Activity | Outcome / evidence |
| --- | --- | --- |
| 2026-10-13 | Reviewed the 129BOB plan/tracker format and Crossclock source | Established existing capabilities and specific latency hypotheses; baseline revision recorded above |
| 2026-10-13 | Researched product workflows and official technical/data documentation | Dated links and resulting recommendations are recorded in the [plan](plan.md#external-research-and-implications) |
| 2026-10-13 | Created plan and initialized tracker | C00 complete; implementation, performance benchmarks and user study remain unstarted |
| 2026-10-13 | Verified documentation links and task mapping | All local link targets exist; C01-C14 match across both documents; tracked diff whitespace check passed; README links to both files |
| 2026-10-14 | Started C01 instrumentation and benchmark implementation | Trace collector, callbacks, queue/persistence/source/cache/retry/transport observation; [benchmark record](benchmarks/c01.md) |
| 2026-10-14 | Verified fixtures and live access within owner-authorized limits | 24 offline samples with no failures; six successful Nemotron and six Jev probes; Jev estimated $0.00007812; raw evidence ignored locally |
| 2026-10-14 | Ran bounded live graph diagnostic and recorded the stop | Three completions, one local NVIDIA send-guard failure, two unstarted; 102 total NVIDIA sends; 32 Jev sends estimated $0.00142107; no account quota change |
| 2026-10-14 | Corrected callback ancestry and verified regressions | 2,018 broad tests passed with one compatibility failure subsequently fixed; 111 final targeted tests passed; 101 raw parent links derived offline with exact originals retained; lint/whitespace checks passed |
| 2026-10-15 | Added startup separation, pair selection, cache journaling and cumulative budget qualification | 68 WSL trace/graph/guard tests and 7 standalone aggregation tests passed; exact config/corpus/workload/lock match; reviewed instrumentation revisions and old dirty-source limitation documented |
| 2026-10-15 | Attempted the two remaining pairs, then made one bounded fresh retry | Cold 2 timed out in Bear Researcher, then Market Analyst; 29 + 17 NVIDIA sends, zero Jev sends; each attempt retained three unstarted rows and stopped new sends |
| 2026-10-15 | Audited all evidence and updated the combined ledger | NVIDIA 148 / Jev 32 total sends including probes; Jev estimate $0.00142107, reserve $0.088080384; all guard/trace counts reconcile; C01 and G1 remain incomplete with two pairs outstanding |
| 2026-10-16 | Retried the original policy, then registered configurable 180-second timeout recovery | Original attempt timed out in Bull Researcher after 23 sends. Recovery persisted three reports and one matched pair; six successful responses exceeded 60 seconds. Final warm run stopped at the local guard; timeout cohorts remain separate |
| 2026-10-16 | Verified timeout controls and reconciled all five attempts | 75 preflight tests, then 44 timeout/aggregation tests passed; Ruff passed. Today: 146 NVIDIA / 31 Jev sends. Cumulative: 294 NVIDIA / 63 Jev; Jev estimate $0.00298578, reservation $0.173408256. Local continuation ceiling exhausted; C01/G1 remain incomplete |
| 2026-10-16 | Added seeded warm completion and per-allowance accounting | Runner `--warm-seed` refuses any hash mismatch before sending; summarizer `--select` pairs across attempts only with a matching cache hash, and `--allowance` caps each allowance separately. 57 focused tests passed; Ruff passed |
| 2026-10-16 | Completed the 180-second baseline under a new owner-approved 144-send allowance | Seeded warm 2, cold 3 and warm 3 completed: 83 NVIDIA / 24 Jev sends, all HTTP 200. Three matched pairs; cold median 371.908 s, warm 435.775 s; no cache-speed claim. Cumulative: 377 NVIDIA / 87 Jev; Jev estimate $0.00428350, reservation $0.239468544. Full non-integration suite: 2,081 passed, 1 skipped (optional Bedrock). Zero credential matches in 827 scanned files. C01 Complete, G1 passed for diagnostic scope |
| 2026-10-17 | C03 design approved as proposed, then built and tested offline | [ADR 0001](adr/0001-bounded-execution-and-run-status.md). 41 C03 acceptance tests passed in WSL, plus focused retry, checkpoint, graph, CLI, report and web suites (264, 129 and 101 passed). One web test is marginal against its 10 s HTTP timeout because the first request imports the openai SDK cold; it took 10.04 s at clean HEAD too. Full non-integration suite: 2,120 passed, 2 failed, 1 skipped (optional Bedrock), 1 deselected. The failures, `test_jev_sentiment.py::test_the_judgments_leave_the_analysts_own_graph_with_its_report` and `test_memory_log.py::TestLegacyRemoval::test_full_pipeline_no_regression`, are not yet investigated; likely fixtures meeting the new `run_checks` output or required-stage gate. C03 stays open until they are resolved. Ruff passed; no live calls |
| 2026-10-19 | Branches consolidated into `main` | `feat/c01-baseline` (C01's last two commits) merged; the C03 work, uncommitted until now, committed to `main`; the merged branches deleted locally and on GitHub, so `main` is the only branch. No tests repeated for this step: the two full-suite failures recorded on 17 October are still uninvestigated and C03 stays In progress |
| 2026-10-19 | Resolved C03's two stale regression fixtures | Sentiment exact output now covers empty and recorded checks; memory regression uses a complete state and the real completion gate. Both failed before editing. Focused suites: 85 and 142 passed. Full Windows Python 3.13 suite: 2,120 passed, 4 skipped, 1 deselected; Ruff and whitespace checks clean. First sandbox run could not reach loopback and placed temp inside scratch HOME; corrected test environment passed with offline external-network guards intact. Raw logs (local, outside the repository): `TradeHFT/.scratch/c-series-20261019/c03-full-access/pytest.log`. No live provider calls; review/merge pending |
| 2026-10-19 | Reran C03's gate independently in Claude Code before review | Tree `78e78cb` with every data path, `HOME` and temp in scratch, `COLUMNS=81`: 2,120 passed, 4 skipped, 1 deselected, 0 failed; Ruff clean; the scratch `HOME` stayed empty. Raw log (local): `TradeHFT/.scratch/c-claude/c03-fix/pytest.log` |
| 2026-10-19 | C04 built and fixture tested | Codex's evidence session, router sharing and news provenance (stashed in the shared checkout at 15:15) were restored and reviewed. Claude Code made each `propagate`/`stream_run` start its own session (`fresh_evidence`; its test failed first, 1 acquisition instead of 3), counted C01's real-graph fixture with sharing off and on (13 -> 12, the Sentiment and News Analysts' shared news window, all 12 routed answers identical), moved the benchmark's runtime into a temporary folder so the summary names no local path, and updated the end-to-end test that pins vendor function names. Planted bugs: 4 of 5 caught; the uncaught one (vendor settings out of the request key) cannot change behaviour, since a session never spans two configurations. At 16:02 the shared checkout committed and pushed this work to `main` as `07220d2` without the end-to-end fix and the C03 fixtures, so the close-out was finished in a separate worktree on top of PR #3. Full offline gate on tree `c7268e2`: 2,225 passed, 4 skipped, 1 deselected, 0 failed; Ruff clean; scratch `HOME` empty. No live calls; Crossclock has no CI runs |
| 2026-10-19 | C05 design proposed | [ADR 0003](adr/0003-research-brief-and-source-drawer.md), source reviewed on top of C04 (PR #4's tree). Findings: each analyst's tool results are discarded with its private message history; C04's evidence records cover 6 of the 12 analyst tools, skip unavailable answers and stay in memory; the claim check is saved only as Markdown; the Reports page drops the report header and never reads `run_status.json`, so an unchecked decision looks checked. Four owner questions; the owner accepted every recommended option the same day. No code, tests or live calls |

## Next update template

```text
Date / timezone:
Task ID / owner:
Status before -> after:
Hypothesis and acceptance criterion:
Code revision / local changes:
Data/evidence manifest, hashes, availability times and rights:
Provider/model/prompt/policy versions, mode and configuration:
Host, concurrency, quotas and cold/warm definition:
Commands and raw artifact paths:
Sample counts, exclusions, failures and uncertainty:
Queue, first-useful-result, completion, retries and source-call results:
Token/Jev usage, pricing version, cost and missing telemetry:
Source support, numeric correctness and event-recall results:
User-task observations, if any:
Evidence class / verdict / limitations:
Blocker and recovery action, if any:
Next action:
```
