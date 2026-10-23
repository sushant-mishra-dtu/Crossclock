# C07 evidence summary: the ten-day checkpoint

Prepared 21 October 2026 (Asia/Calcutta) by Claude Code on `main` `45d1213`, for the owner's
C07 decision. Documentation only: no code, tests, live calls or spend went into it.

**C07 is the owner's decision. This document records the evidence and the options; it decides
nothing.** The one recommendation in it is labelled as such ([section 6](#6-recommendation)).

Every figure below is quoted from the record named beside it. Nothing here is a new measurement.
Evidence classes are those of [progress.md](../progress.md#status-definitions): **source
reviewed**, **external source reviewed**, **fixture tested**, **live measured** and **user
observed**. No figure is moved up a class: a fixture count stays a fixture count.

## What C07 asks for

[plan.md](../plan.md#delivery-sequence), C07: "Review C01-C05 and any C06 evidence; record
continue, revise or defer with measured benefits and remaining quality gaps." The plan adds:
"C07 chooses the next P1 slice."

[Success rule](../plan.md#decision-and-progress-rules): "C07 succeeds when the baseline is
reproducible, at least one meaningful improvement is measured, evidence quality is preserved and
the next feature choice is supported by user-task findings or an explicit remaining hypothesis.
Missing the aspirational timing target requires an honest revision, not relabeling partial
output as complete."

The owner records three things: continue, revise or defer; the next P1 slice; and an answer to
each open question, or "left open" ([section 7](#7-recording-the-decision)).

## In short

- **Baseline:** reproducible within its diagnostic scope (G1 passed): live model calls on
  synthetic evidence, one instrument. It cannot be repeated on today's `main` without a new
  registered cohort and a live allowance, because C03 changed the configuration hash.
- **Measured improvement: none.** The last live run was on 16 October (C01). C03-C06 are fixture
  tested only. C04's 42 -> 18 acquisitions is a count on synthetic sources, not a measurement.
- **Evidence quality:** not measured either way. No claim set, numeric audit or research-quality
  comparison exists ("Not audited"; the quality reference set is "Not created"). What exists is
  fixture evidence that the changed paths return the same answers and keep partial and unchecked
  states visible.
- **User-task findings: none.** The owner skipped C02's interviews for now. The next feature
  choice can rest only on an explicit hypothesis.

Read against the success rule, criteria 2 and 3 are not met and criterion 4 is met only if the
owner records a hypothesis ([section 2](#2-against-the-c07-success-criteria)).

## 1. C01-C06, task by task

| Task | Tracker status ([progress.md](../progress.md#task-tracker)) | Evidence class | Live measured | User observed |
| --- | --- | --- | --- | --- |
| C01 baseline | Complete (diagnostic scope) | Live measured on synthetic sources; fixture tested | Yes, synthetic evidence only | No |
| C02 user tasks | Not started | None | No | No |
| C03 bounded execution | In progress: offline gate passed | Source reviewed; fixture tested | No | No |
| C04 shared evidence | In progress: fixture tested; code reviewed, no defect | Source reviewed; fixture tested | No | No |
| C05 brief and drawer | In progress: built, fixture tested, walked through | Source reviewed; fixture tested | No | No |
| C06 modes, Quick | In progress: first slice; Deep deferred | Source reviewed; fixture tested | No | No |

### C01: instrumentation and baseline

**Built.** Run and span tracing of queue, graph nodes, model and Jev calls, HTTP sends, retries,
source requests, cache hits and persistence; a benchmark runner with send guards, per-allowance
accounting and seeded warm runs ([c01.md](c01.md#trace-contract)).

**Evidence class.** Live measured: real Nemotron 3 Super and Jev 1.13 calls, on a frozen,
invented CC0 corpus with one instrument identifier (NVDA). Fixture tested: the offline runs.

**Records.** [c01.md](c01.md), [c01-summary.json](c01-summary.json).

**Figures** (all from [c01.md](c01.md) unless noted):

- 180-second cohort, research setup to persisted report: cold median 371.908 s (341.489-417.197 s),
  warm median 435.775 s (285.266-494.657 s), n=3 each; p95 unqualified
  ([metrics ledger](../progress.md#metrics-ledger)). This is the baseline of record.
- "The warm source cache shows no speed benefit": each fixture fetch sleeps 10 ms, so the cache
  can remove at most about 0.3 seconds per run, while run-to-run variation is several minutes.
- The original 60-second cohort keeps one matched pair (cold 189.327 s, warm 175.019 s), not
  pooled.
- Across all six graph attempts: 9 completed, 5 failed, 11 unstarted.
- Across both recovery attempts, 206 NVIDIA responses returned; 11 took more than 60 seconds; the
  longest took 125.456 seconds.
- 9 of the selected cohort's 187 generations stopped at the 2,048-token limit.
- "The six final ratings were Sell, Hold, Buy, Sell, Buy and Underweight on identical frozen
  evidence at temperature 1.0."
- Largest cold waits (n=3): Market Analyst 79.646 s, Bear Researcher 62.394 s, Bull Researcher
  57.618 s. The selected first optimization (shorter, source-grounded bull/bear arguments) was
  not run.
- Dependency startup, measured separately: 79.012 s, 57.143 s and 174.749 s.
- Spend, all attempts and probes: 377 NVIDIA sends and 87 Jev sends; Jev estimate $0.00428350,
  conservative reservation $0.239468544, under the owner's US$1 ceiling; NVIDIA dollar cost and
  remaining quota unknown.

**Unverified.** Real-source timing; rendered first-useful-result; the plan's 12-instrument
NSE/US matrix; queue wait and a 10-symbol queue; qualified tails; quality; NVIDIA dollar cost.
No allowance remains: "the 192-send continuation is used up, and the 144-send completion
allowance belonged to its single attempt" ([progress.md](../progress.md#open-decisions-and-prerequisites)).

### C02: user tasks and data-access inventory

**Not started.** Owner decisions of 20 October
([progress.md](../progress.md#owner-decisions-20-october-2026)): the target cohort is "Indian
retail investors, plus small advisers and analysts", replacing the NSE watchlist investor
hypothesis; interviews are "skipped for now. The owner will hold them when possible and report
the results. Until then, every user-task claim stays a hypothesis: there is no user evidence."
No data-rights review exists: "No deployment-specific entitlement review performed".

### C03: deadlines, retries, Stop and partial states

**Built.** One run controller; a 1,200 s Standard deadline; a 180 s attempt timeout for every
provider; one retry layer with deadline-aware, interruptible waits; a guard that refuses every
HTTP send after Stop or the deadline; a completion record (`complete`, `complete_with_gaps`,
`partial`, `none`) with a stop reason; partial reports with no rating and no decision
([ADR 0001](../adr/0001-bounded-execution-and-run-status.md)). On 20 October the post-screen Jev
fallback was brought under Stop and the deadline (PR #11).

**Evidence class.** Source reviewed (including offline SDK inspection) and fixture tested.

**Records.** [ADR 0001](../adr/0001-bounded-execution-and-run-status.md),
[acceptance tests](../../tests/test_c03_run_control.py),
[post-screen tests](../../tests/test_c03_post_screen.py).

**Figures.** 41 C03 acceptance tests passed (activity log, 17 October); full offline gate 2,120
passed, 4 skipped, 1 deselected (19 October). Post-screen fix: "Four of five new tests failed
before the fix"; related suites 120 passed ([progress.md](../progress.md#activity-log)); full
offline gate 2,391 passed on tree `72e4efe`
([PR #11](https://github.com/sushant-mishra-dtu/Crossclock/pull/11)).

**What it changes, in its own class.** Source review found that before C03 one logical model call
"could take about 57 minutes (18 x 180 s attempts + 190 s outer waits + about 9 s of SDK
backoff)", and that "This is a source-derived bound, not an observation" (ADR 0001). After C03 the
run deadline caps retries, which the offline tests show. That is a bound on the worst case, not a
measured speedup.

**Unverified.** "Not live measured: no provider call, no browser walkthrough, deadline-miss rate
unknown" ([tracker](../progress.md#task-tracker)); whether 180 s and 1,200 s suit real sources.
Known limits (ADR 0001): a request in flight is not aborted; Google and Bedrock transports are not
hooked; Jev's own retry sleeps (at most 5 s) cannot be interrupted; `AnthropicClient` forwards
`http_client` into `model_kwargs`, found and not fixed.

### C04: shared evidence and bounded retrieval

**Built.** One evidence session per run: identical requests fetch once, concurrent callers share
one in-flight request, failures and unavailable answers are never reused, 60 s reuse for price
and news and 300 s for financials, at most two acquisitions in flight per source; news items keep
their URL and UTC publication time ([ADR 0002](../adr/0002-shared-evidence-and-bounded-retrieval.md)).

**Evidence class.** Source reviewed and fixture tested; code reviewed on 20 October with no
correctness defect.

**Records.** [ADR 0002](../adr/0002-shared-evidence-and-bounded-retrieval.md) and its
[review](../adr/0002-shared-evidence-and-bounded-retrieval.md#review-20-october-2026);
[c04-summary.json](c04-summary.json).

**Figures** (ADR 0002 and the summary):

| Workload | Without sharing | With sharing | Answers |
| --- | --- | --- | --- |
| Seven-read router workload, NVDA and RELIANCE.NS, 3 repetitions each | 42 acquisitions | 18 | All 42 identical |
| C01's real-graph fixture (scripted model, four analysts, Jev off) | 13 | 12 | All 12 routed answers identical; same rating |

ADR 0002 says the first workload "is built to overlap and shows the most a run can save"; the one
repeat in the real graph is the news window the Sentiment and News Analysts share. The summary's
own interpretation field: "Synthetic source-count and exact-evidence checks only; no latency,
speedup, cost, real-source coverage or model-quality claim." 105 offline tests; planted bugs 4 of
5 caught (the uncaught one cannot change behaviour, since a session never spans two
configurations).

**Unverified.** Latency and cost; the live duplicate rate; the queueing cost of the two-per-source
cap; reuse across runs (C08). Its acceptance asks for a "measured request reduction without
coverage loss": that exists on fixtures only. Three owner questions are open
([section 4](#4-open-owner-questions)).

### C05: brief and source drawer

**Built.** Every tool result and prefetched feed an analyst reads becomes an evidence entry
(gaps included), stored content-addressed; the claim check is saved as data; reports gain
`evidence.json`, `claim_check.json` and a Sources appendix; a saved report opens on a brief
assembled in code, with a keyboard- and mobile-ready source drawer; no prompt, model or graph edge
changed ([ADR 0003](../adr/0003-research-brief-and-source-drawer.md)).

**Evidence class.** Source reviewed (design) and fixture tested (build and walkthrough).

**Records.** [ADR 0003](../adr/0003-research-brief-and-source-drawer.md),
[c05-walkthrough.json](c05-walkthrough.json).

**Figures.** 40 new tests, 14 of 14 planted bugs caught; full offline gate 2,265 passed; the
walkthrough passed 31 of 31 checks at 1440 px (both themes) and 375 px, keyboard only, no console
errors, on three synthetic reports from one offline graph run; it found three faults, fixed
([progress.md](../progress.md#current-position)).

**Unverified.** "Fixture tested only: no live run and no user has used it." The walkthrough is a
scripted browser, not a user, so the plan's "A user can inspect sources" is shown for the
interface, not observed. Statement-to-source links are made in code (the section's sources plus a
figure trace); the claim check compares the thesis with the analyst reports, not with the
original sources (ADR 0003 context). Whether a source actually supports a statement is unchecked.

### C06: research modes and the first Quick path

**Built.** Quick and Standard; Deep shown as planned and unavailable. Quick is a separate runner:
local point-in-time facts, one 30-day price and one 7-day news window, at most one tool-free
synthesis with no retry and a 1,200-token cap, code-only citation and figure checks, a 60 s active
budget, and no rating, decision memory or Standard artifact
([ADR 0004](../adr/0004-research-modes-and-quick-path.md)). Codex built it; Claude Code verified
it through the real spawned workers against a loopback fake provider and fixed two defects
([PR #9](https://github.com/sushant-mishra-dtu/Crossclock/pull/9)).

**Evidence class.** Source reviewed (design) and fixture tested.

**Records.** [ADR 0004](../adr/0004-research-modes-and-quick-path.md),
[c06-walkthrough.json](c06-walkthrough.json),
[real-worker tests](../../tests/test_c06_worker_e2e.py),
[PR #9](https://github.com/sushant-mishra-dtu/Crossclock/pull/9).

**Figures.** Codex's build: 115 C06 tests, "8 of 8 planted boundary faults caught"; after
verification 121 offline C06 tests; full gate 2,386 passed on tree `899c0c1` (PR #9). Walkthrough
127 of 127, both themes, 1440 px and 375 px, keyboard only; provider sends "exactly one in each of
the 9 runs", maximum output 1,200 tokens, no tools (walkthrough record). Defects fixed test-first:
a cited price could never be traced (CSV `99,101.5` read as one number), and a synthesis provider
failure was saved as a generic `error` (now `provider_error`). The trade-instruction screen
withholds the whole synthesis on plain factual sentences in 4 of 9 probes.

**Timing, fixture only.** First rendered price row 2,030-2,042 ms after submit in the four
viewports; the record's caveat: "It is not a measured Quick latency and qualifies no target."

**Unverified.** "Fixture tested only: no live run, no user and no latency claim." The plan's
Quick objectives (warm first facts <=5 s p50 / <=10 s p95; brief <=30 s / <=60 s) are unmeasured;
qualitative support is shown as "source support not checked"; anticipated cost is shown as
unavailable; Deep is unavailable, so "Full C06 stays open until targeted Deep retrieval exists".

## 2. Against the C07 success criteria

| Criterion ([plan](../plan.md#decision-and-progress-rules)) | Recorded evidence | Reading |
| --- | --- | --- |
| The baseline is reproducible | G1 "Passed for the synthetic-source diagnostic baseline: three matched 180-second pairs, verified cache seeds, reconciled ledgers"; commands, hashes and selections in [c01.md](c01.md#reproduction-and-evidence) | **Met within its diagnostic scope.** One synthetic instrument, not the NSE/US matrix. Since C03 "C01 benchmark-runner runs now get a new configuration hash; the warm-seed check refuses C01 caches", so today's code needs a registered post-C03 baseline (a live allowance) or an explicit policy note ([prerequisites](../progress.md#open-decisions-and-prerequisites)) |
| At least one meaningful improvement is measured | No live run after 16 October. C03-C06 fixture tested. C04: 42 -> 18 and 13 -> 12 acquisitions on synthetic sources. C06: a 2 s fixture-only offset. G3 "Not evaluated" | **Not met.** No improvement is live measured. The Standard speed target (>=30% lower p50 and >=20% lower p95 than the matched baseline) is not measured, so its actual effect is unknown, not zero |
| Evidence quality is preserved | Supported-claim share, numeric errors, event recall and claim accuracy all "Not audited"; quality reference set "Not created". Fixture evidence: C04's answers identical (42 of 42; 12 of 12 and the same rating); C03 and C05 keep partial and unchecked states visible; C06 keeps decision fields out of Quick | **Not met as a measurement.** The fixtures show that the changed paths return the same answers and label gaps; they do not measure research quality. Against it: C01's six ratings on identical evidence; C05's links are code-only; Quick's qualitative support is unchecked |
| The next feature choice is supported by user-task findings or an explicit remaining hypothesis | C02 not started; interviews skipped by the owner; G4 "Not evaluated"; human task time "No participants observed" | **No user-task findings.** Met only if the owner records the choice as an explicit hypothesis (for example the 20 October cohort) |

### The metrics ledger at C07

From the [metrics ledger](../progress.md#metrics-ledger); "Not measured" and "Not audited" mean
unknown, never zero.

| Metric | Baseline | Candidate | At C07 |
| --- | --- | --- | --- |
| Warm first useful facts, p50 / p95 | Not measured | Quick built; not measured; fixture-only offset about 2 s | No result |
| Quick completed brief, p50 / p95 | No Quick path | Built and fixture tested; not measured | No result |
| Standard complete report, p50 / p95 | Cold 371.908 s, warm 435.775 s, n=3 each, synthetic sources; p95 unqualified | Not measured | No comparison |
| Cold first visit and final report | Dependency startup 79.012, 57.143, 174.749 s; browser first visit unknown | Not measured | No comparison |
| Queue wait and 10-symbol completion | Not measured | Not measured | No result |
| Report cost and failed-run spend | Jev estimate $0.00428350; NVIDIA dollars unknown; 377 NVIDIA sends | Not measured | No comparison |
| Source-request count | 42 and 13 (synthetic) | 18 and 12, fixture tested | Fixture count only; live duplicate rate needs an allowance |
| Supported or explicitly unsupported claims | Not audited | Not audited | No result |
| Audited material numeric errors | Not audited | Not audited | No result |
| Material-event recall / claim accuracy | Not audited | Not audited | No result |
| Completion / partial / failure / deadline-miss rates | 9 completed, 5 failed, 11 unstarted (not operational rates) | Not measured; C03 labels each run | No result |
| Human research-task time | No participants observed | No participants observed | No result |

### The gate ledger at C07

Quoted from the [gate ledger](../progress.md#gate-ledger): G1 passed for the synthetic-source
diagnostic baseline; G2 "Partly evaluated", offline only (C03-C06), needing "Live confirmation of
deadline and Stop behavior, and of source freshness on real vendors"; G3 "Not evaluated"; G4 "Not
evaluated"; G5 "Deferred".

## 3. Phase R: how later claims will be judged

Phase R is the evaluation-hardening track the user chose on 11 October. It is tracked outside this
repository, in the workspace's own progress file, and each task is built test-first. It judges
claims about the **outcomes of Crossclock's ratings** made with `backtest` and `learn`; it does
not judge research speed or brief quality. The C-series makes no return claim, and the plan says
"Faster research is not evidence of a profitable strategy" ([plan](../plan.md#measurement-and-quality-gates)).

| ID | What it adds | State |
| --- | --- | --- |
| R1 | Brier score and a held-out reliability table beside log loss in `learn` ([outcome_model.py](../../tradingagents/outcome_model.py)) | On `main`: [PR #12](https://github.com/sushant-mishra-dtu/Crossclock/pull/12), `68d2a56` |
| R2 | Each directional call scored net of one round trip in the `backtest` summary: direction x alpha - cost; defaults 30 bps for NSE/BSE delivery and 5 bps for US stocks, a 30 bps stand-in elsewhere; `--cost-bps`; a warning below 10 bps, the log's precision ([backtest.py](../../tradingagents/backtest.py)) | On `main`: [PR #13](https://github.com/sushant-mishra-dtu/Crossclock/pull/13), `1ea157c` |
| R3 | A trial ledger, `<results_dir>/trials.jsonl`, and the probabilistic and deflated Sharpe ratios of following the ratings; N counts distinct configurations of the same kind on overlapping dates; at least 6 periods ([sharpe.py](../../tradingagents/sharpe.py), [trials.py](../../tradingagents/trials.py)) | On `main`: [PR #14](https://github.com/sushant-mishra-dtu/Crossclock/pull/14), `45d1213` |
| R4 | `price only` and `rating + price` models in `learn`: no LLM, six features from closes before each analysis date | [PR #15](https://github.com/sushant-mishra-dtu/Crossclock/pull/15), open, waiting for the user to merge |
| R5 | Combinatorial purged cross-validation and the probability of backtest overfitting in `learn` | Not started (after R4) |
| R6 | Results before and after each model's training cutoff | Spec after R5 |
| R7 | R1-R5 on one real backtest and `learn` run | Needs the user: a paid backtest sweep and the Jev key |

What this means for later claims:

- A claim that following the ratings pays will be expected to be net of costs (R2), to state how
  many configurations were tried and its deflated Sharpe ratio (R3), to beat a no-LLM price
  baseline (R4, once merged) and to be calibrated (R1); R5 and R6 will add a spread over splits,
  an overfitting probability and a training-cutoff split.
- None of R1-R4 has run on real data yet; every test uses synthetic data, and R7 needs the owner.
- C01's six ratings on identical evidence (Sell, Hold, Buy, Sell, Buy, Underweight) bear on both
  tracks: an outcome score and a C08 "what changed" comparison each have to separate run-to-run
  spread from a change in evidence.
- Speed claims have their own rules in the [plan](../plan.md#measurement-and-quality-gates):
  matched pairs, at least three repetitions for diagnosis, at least 100 comparable runs before a
  p95 is called achieved, no pooling of unlike providers, and failed runs shown. The C-series has
  no trial ledger like R3's; its safeguard is registering each workload and allowance before a
  live run, as C01 did.

## 4. Open owner questions

| Question | Recorded state | Options as recorded | What waits on it |
| --- | --- | --- | --- |
| How far to narrow Quick's trade-instruction screen | Conservative, as ADR 0004 asks: one match withholds the whole synthesis. It matched 4 of 9 plain factual probes: "Promoters hold 51% of the shares", "Analysts were bullish after the results", "The company will buy back shares", "Insiders sell shares regularly" ([prerequisites](../progress.md#open-decisions-and-prerequisites)) | Keep it, or narrow it to recommendation and instruction forms, tested against a set of planted trade proposals and plain facts | How often Quick returns a synthesis at all; no live measurement either way |
| [ADR 0002's questions](../adr/0002-shared-evidence-and-bounded-retrieval.md#questions-for-the-owner) | Per-run scope; 60 s (price, news) and 300 s (financials); two acquisitions in flight per source | (1) keep the per-run scope or allow reuse across runs within the freshness limits; (2) are 60 s and 300 s right, given runs of 6-7 minutes in C01; (3) keep the cap until a live run measures its cost | Question 1 is C08's central design choice; question 3 needs a live run |
| Runs without Jev read "complete with gaps" | Without `TYPESAFE_API_KEY` every complete run reads "complete with gaps", as approved | Keep it, or treat an unconfigured Jev as switched off (not a gap) | How Standard runs without Jev are labelled |
| The post-C03 baseline | C03's three configuration keys give post-C03 runs a new configuration hash; no live allowance remains | Register a post-C03 baseline (needs a live allowance), or compare against C01 with an explicit policy note | Any measured improvement (criterion 2), including C01's selected bull/bear experiment |

Also open, recorded elsewhere: C02's interviews (the owner holds them when possible); product and
study owners ("remain open"); the data-rights review before any hosting; Phase R's choices listed
in PRs #14 and #15 and R2's 30 bps stand-in for other markets; the merge of PR #15.

## 5. Options for the next P1 slice

Prerequisites are as stated in the [plan's delivery sequence](../plan.md#delivery-sequence) and
the [tracker](../progress.md#task-tracker). "Prerequisite met" means built and fixture tested; none
is live measured.

| Option | Stated prerequisites | Met today |
| --- | --- | --- |
| C08 report versions and incremental research | After C04-C07 | C04 built (In progress, owner questions open); C07 is this checkpoint |
| C09 filing retrieval and Q&A | After C04/C05 | Both built (In progress) |
| C10 thesis notebook and digest | After C08 | No: C08 not started |
| C11 comparisons, portfolio impact, query drafts | After C05/C08; demand from C02 | No: C08 not started, no C02 evidence |
| C12 shared scheduler and throughput | After C01/C03/C04; "timing depends on measured queueing" | Tasks built; queueing not measured |

**C08: versioned reports and incremental research.**
For: C05 already saves `evidence.json`, `claim_check.json` and content-addressed texts with
hashes, which is what the plan's rule "compare structured facts and source hashes before asking an
LLM to explain the difference" needs; C10, C11 and C13 all list C08 as a prerequisite; it can be
built test-first offline with no spend. Against: ADR 0002's first question (reuse across runs) is
unanswered and is C08's core design choice; C01's rating spread on identical evidence means a
comparison must keep model variance apart from new evidence, which the plan requires ("distinguish
model/config changes from new company evidence"); how often runs or users repeat research is
unmeasured; "a new filing produces an accurate comparison" needs filings, and Indian filings reach
the app only through `india import` of files the user saved in a browser (the workspace's
data-source rule).

**C09: filing retrieval and source-grounded Q&A.**
For: its prerequisites are built; full C06 acceptance waits for targeted Deep retrieval, "which
overlaps C09"; the plan starts with SEC filings, for which an adapter exists. Against: its
acceptance needs held-out questions with correct passages, and the quality reference set is "Not
created"; the chosen cohort is Indian retail investors, whose filings arrive only by manual
import; "Historical and transcript coverage: Incomplete/unverified"; the plan says "Do not index
documents the application cannot lawfully obtain/use", and no rights review exists.

**C10: thesis notebook and digest.** For: it extends existing alerts and watchlists and is a P1
feature. Against: its stated prerequisite, C08, is not started.

**C11: comparisons, portfolio impact and query drafts.** For: it reuses existing peers, holdings
and the query compiler. Against: two of its prerequisites are unmet (C08, and demand from C02).

**C12: shared scheduler and throughput.** For: source review found the batch queue is serial and
direct analyses start separately ([baseline findings](../progress.md#baseline-findings)). Against:
it is not one of the plan's P1 user features; its trigger, measured queueing, does not exist
("Queue wait and 10-symbol completion: Not measured").

**Not a P1 slice: a measurement step.** Register a post-C03 Standard baseline and measure what
C03-C06 built, under a recorded allowance: Quick's first facts and completion, C04's live
duplicate rate and the cap's queueing, live Stop and deadline behaviour, then C01's selected
bull/bear experiment against that baseline. For: it is the only route to criterion 2 and the
evidence G2 and G3 ask for. Against: it needs owner-approved spend (NVIDIA quota unknown; Jev
reserved $0.239468544 of US$1 so far); runs on the synthetic corpus still give no real-source
timing; real NSE data stays bound by the data-source rule.

## 6. Recommendation

**Recommendation (Claude Code's, for the owner to accept, change or reject; not a decision):**

1. Record C07 as **revise**. Keep C03-C06 as built and record plainly that no improvement is
   measured yet and that research quality is unaudited, rather than reading the fixture counts as
   benefits.
2. Approve a small, registered live allowance for the measurement step, starting with the
   post-C03 baseline, so the next checkpoint can report a measured effect, or its absence.
3. Choose **C08** as the next P1 slice, after answering ADR 0002's first question. Its
   prerequisites are the closest to met, it builds on C05's saved records with no new spend, and
   three later tasks wait on it. Take C09 next if full C06 (Deep) matters sooner.
4. Record the 20 October cohort (Indian retail investors, plus small advisers and analysts) as the
   explicit remaining hypothesis behind that choice until C02's interviews report.

## 7. Recording the decision

The owner's answer goes in [progress.md](../progress.md): the C07 row, a dated entry under the
owner decisions, and an activity-log row; scope changes go in [plan.md](../plan.md). A template:

```text
C07 decision, <date>:
Outcome: continue / revise / defer
Next P1 slice:
Measured benefits accepted at C07:
Remaining quality gaps acknowledged:
Explicit remaining hypothesis:
Quick screen: keep / narrow / left open
ADR 0002 questions 1-3:
Runs without Jev:
Post-C03 baseline and live allowance:
```

## Sources

On `main` `45d1213`: [plan.md](../plan.md), [progress.md](../progress.md),
[ADR 0001](../adr/0001-bounded-execution-and-run-status.md),
[ADR 0002](../adr/0002-shared-evidence-and-bounded-retrieval.md),
[ADR 0003](../adr/0003-research-brief-and-source-drawer.md),
[ADR 0004](../adr/0004-research-modes-and-quick-path.md), [c01.md](c01.md),
[c01-summary.json](c01-summary.json), [c04-summary.json](c04-summary.json),
[c05-walkthrough.json](c05-walkthrough.json), [c06-walkthrough.json](c06-walkthrough.json).
On GitHub: [PR #9](https://github.com/sushant-mishra-dtu/Crossclock/pull/9) (C06 verification) and
Phase R's [#12](https://github.com/sushant-mishra-dtu/Crossclock/pull/12),
[#13](https://github.com/sushant-mishra-dtu/Crossclock/pull/13),
[#14](https://github.com/sushant-mishra-dtu/Crossclock/pull/14) and
[#15](https://github.com/sushant-mishra-dtu/Crossclock/pull/15). Outside the repository: the
workspace's Phase R tracker and handover (R1-R7 states, the data-source rule).
