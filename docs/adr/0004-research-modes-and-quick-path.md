# ADR 0004: Research modes and first Quick path (C06)

Status: **Proposed**, 20 October 2026 (Asia/Calcutta). C06 in the
[plan](../plan.md#research-modes) and [tracker](../progress.md). Evidence:
source reviewed on C05 merge `cc97989` (C05 implementation `96926c9`);
no implementation, provider calls, speed measurements or user observations.
The tracker requests an owner design before implementation, as for C03/C05.

## Context

The existing controls describe three different things:

| Control | Current meaning | Consequence |
| --- | --- | --- |
| `research_mode` | Internal budget selection; only Standard exists | Add validated product modes without changing old requests' default |
| Research depth: Shallow / Medium / Deep | 1 / 3 / 5 research and risk discussion rounds | Shallow is still a full decision; never migrate it to Quick |
| Quick-thinking / Deep-thinking models | Model tiers used by different agents | Preserve their keys and explain that these are model roles, not research modes |

Source findings:

- [Graph setup](../../tradingagents/graph/setup.py), `setup_graph`, always
  joins the selected analysts and Memory Log into research discussion, Trader,
  risk discussion and Portfolio Manager. Selecting fewer analysts cannot make
  Quick. The [graph constructor](../../tradingagents/graph/trading_graph.py)
  also initializes both model tiers and memory before a run starts.
- [Completion](../../tradingagents/completion.py), `required_stages` and
  `run_completion`, requires the full decision pipeline. `is_complete` checks
  completion state alone; it is not a decision-eligibility check. Initial
  `past_context=""` can make memory look as though it ran even when omitted.
- Both [graph execution](../../tradingagents/graph/trading_graph.py),
  `_run_graph`, and [web jobs](../../cli/webui/jobs.py), `_run_traced`,
  have success paths that record a decision and extract a rating. Every caller
  needs the mode boundary, including the CLI, direct library use and queues.
- C04's [evidence session](../../tradingagents/dataflows/evidence.py) is per
  run. Its waits check Stop/deadline, but a running source loader must bound
  its own transport. C05's content-addressed texts are audit storage, not an
  eligible cache for a fresh Quick run.
- The current [company profile](../../tradingagents/dataflows/vendors/india/profile.py),
  `build_company_profile`, is a live view; [company inputs](../../tradingagents/screener/company.py),
  `load_company`, support point-in-time reads. Do not use today's company
  profile as facts for a historical request.
- [Backtest submission](../../cli/webui/server.py), `start_backtest`, shares
  settings parsing with Analyze. A mode remembered globally could leak Quick
  into a workflow that requires a decision.
- C05's [brief](../../tradingagents/brief.py) reads Portfolio Manager output;
  [report discovery](../../cli/webui/jobs.py), `list_saved_reports`, knows
  complete/partial decision reports and state logs. Quick needs its own output
  fields and artifact kind, while retaining the source drawer.

## Proposal

### 1. Modes and compatibility

| Mode | First C06 slice | Work and completion meaning |
| --- | --- | --- |
| Quick | Selectable | A sourced company snapshot and recent headlines, one concise synthesis, deterministic checks; a completed brief, no investment decision |
| Standard | Default and selectable | Current selected specialists, research/risk discussion and decision pipeline; unchanged prompts, models, explicit round overrides and 1,200 s / 180 s default policy |
| Deep | Visible as planned, unavailable | Requires targeted document retrieval and work driven by unresolved evidence, with an explicit user-selected budget; extra rounds alone do not implement it |

The recommended first slice delivers Quick/Standard execution and an honest
Deep control. **This does not complete C06's full three-mode acceptance.** Keep
C06 In progress and record the Deep dependency/scope decision. The alternative
is to expand C06 to implement and evaluate targeted retrieval now; that is a
larger task overlapping C09.

Missing mode on an old request means Standard. Unknown modes and unavailable
Deep are rejected before creating a job or sending a request. A hidden or
disabled browser option alone is insufficient. Preserve legacy depth as an
advanced Standard round-count control and show effective rounds; call a run
with changed rounds customized Standard. Remove the CLI's current description
of Shallow as "Quick research". Model-tier keys remain compatible.

### 2. First Quick workload

Proposed policy `quick-v1` is fixed and shown before Run:

1. Resolve canonical ticker/exchange and requested date. Read eligible local
   facts first, without triggering remote company-profile lookup. Unknown
   company names, periods, currency and units stay unknown.
2. Acquire at most one price window (last 30 calendar days) and one news
   window (last 7 calendar days) through the existing source routing and
   provenance machinery. Keep at most 8 dated, in-window headlines. Use
   point-in-time local financial statements when available; remote financial
   statements, social feeds and filing retrieval are outside this first scope.
   Missing local financials, including on US symbols, are an explicit gap.
3. Render sourced local facts as they become available, ahead of synthesis.
   A fact needs its date, source and unit/currency where known. If no eligible
   local facts exist, say so; a spinner is not a first useful result. Reuse
   requires the source's freshness/as-of rules, not simply a file on disk.
4. Make at most **one tool-free generation request**, using the configured
   quick-thinking model only, with no repair/fallback generation or retry.
   Pass at most 24,000 characters of evidence, keeping evidence IDs, dates and
   explicit truncation/omission notices. Request at most 1,200 output tokens;
   record any provider limitation on enforcing that cap. The summary cites
   only the supplied evidence IDs and covers observations, gaps and questions
   that would benefit from Standard. Source text is untrusted input.
5. Validate cited IDs against this run's manifest and trace numerical claims
   to source passages using C05's precision rules. Invalid references are
   flagged and never become clickable sources. Figure presence does not
   establish qualitative support. Display "source support not checked" for
   qualitative claims; no Jev entailment or full decision claim check runs.

The accepted synthesis schema contains only observations with evidence IDs,
gaps and research questions. It has no action, rating, price target, position
size, entry or stop fields. Reject extra decision fields and conservatively
screen generated text for recognizable ratings, trade instructions, targets
and sizing recommendations. Never render raw rejected output as a fallback.
An out-of-scope proposal with otherwise valid citations withholds the entire
synthesis, leaves sourced facts visible and makes the brief partial with an
explicit validation reason. These guards are not a claim of complete semantic
verification; qualitative support remains unchecked.

Quick's policy enforces zero retries at SDK and outer-loop levels and caps
output at 1,200 tokens (or a smaller explicit cap). Inherited retry/token
defaults cannot enlarge that policy. Analyst selections, debate/risk/tool
round counts and deep-tier settings are Standard-only and shown as outside
Quick scope. Existing language/provider/quick-model choices still apply.

No specialist tool loops, decision memory read/settlement/write, research/risk
discussion, Trader or Portfolio Manager runs. A user can start a **new**
Standard run explicitly; Quick never silently upgrades itself or consumes a
Standard budget. Existing local facts with historical/as-of uncertainty are
withheld or visibly unavailable, not described as current verified facts.

### 3. Budget and cancellation

Recommended initial active budget: **60 seconds**, with at most 15 seconds for
initialization/identity/local reads/acquisition, at most 40 seconds for the one
generation attempt and 5 seconds reserved for checks/persistence. Set the work
cutoff to the run deadline minus that reserve. Acquisition ends at the earlier
of start + 15 s and the work cutoff; generation's timeout is the smaller of
40 s and time left to the work cutoff. Do not start it if that time is
nonpositive. Queue wait is recorded separately, as in C03.
These are proposed stop budgets, **not measured ETAs or a completion promise**.
The plan's 5/10-second facts and 30/60-second brief objectives remain unproven.

Identity lookup, source transports and model initialization must be included
in bounded active work. A timeout on `Future.result` is insufficient: the
source may continue after the caller stops. Audit every enabled Quick adapter;
apply remaining-time transport limits, or use an independently terminable
worker for a loader that cannot enforce them. Preserve source configuration,
per-host rules, cancellation, provenance and scratch paths across that worker
boundary. Do not enable a loader with an unbounded lifetime. No detached
network activity or late artifact mutation by acquisition/generation workers
may survive Stop/work cutoff. The coordinator alone may finish cleanup and
save partial output within the reserved allowance (at most 5 s after a manual
Stop, and within the run deadline for a scheduled cutoff). Record a persistence
failure explicitly rather than labelling an unsaved brief complete; filesystem
failures are not a basis for claiming a strict wall-clock completion guarantee.

The existing NSE host, rate and user-agent restrictions still apply. Quick
does not add a filing acquisition route. Slow or unavailable evidence yields
an honest partial/with-gaps result; no extra source fallback may exceed the
registered acquisition scope or deadline.

One request and an output cap bound work, not dollars. Show anticipated cost
as **unavailable** until a comparable measured range or versioned pricing model
exists. Preserve unknown usage/cost as unknown, including failed requests;
record pricing version/currency when estimates exist. A strict monetary cap
would additionally need cost reservation before every send. No live spend is
authorized by this ADR; C01's allowances were specific to its attempts.

### 4. Completion and decision boundary

Use one validated mode policy at every entrypoint, branching before full
`TradingAgentsGraph` construction. A small Quick runner owns its distinct
topology/output, while reusing C03 controls, C04 routing and C05 provenance
and persistence helpers. Extend the existing job/queue path; no second queue
or scheduler is introduced.

Persist mode, output kind, policy/schema version, intended and actual scope,
effective model/provider, budgets and checks. Quick has explicit
`decision_eligible=false`, `rating=null`, `quick_summary` and `quick_checks`.
Only its required outputs count toward brief completion. The full Standard
required-stage gate stays intact. Checks omitted by design are labelled
"not in Quick scope", not "ran"; attempted failed checks remain gaps.

- Usable sourced output, synthesis and deterministic checks completed: "Quick
  brief complete", or "Quick brief complete with gaps" for missing inputs or
  flagged checks. Always show "No investment decision produced".
- Deadline, Stop, provider failure or missing synthesis: partial Quick output,
  retaining obtained facts/evidence and explaining what stopped.
- No usable evidence: no completed brief and no manufactured summary.

Guard decision eligibility at every decision/memory/state-log writer, rather
than relying only on missing Portfolio Manager fields. Poisoned Quick state
or a stale checkpoint must not produce BUY/SELL/HOLD/REVIEW, a paper-trade
proposal, a decision-memory entry or a `full_states_log_*.json` artifact.
Backtest UI uses Standard explicitly and the API/library reject Quick (and
unavailable Deep) before a paid send. No sibling importer change is in C06.

### 5. Saved output, controls and resume

Quick writes `quick_brief.md`/HTML, its own structured brief data,
`run_status.json` and C05-compatible `evidence.json` in its report directory.
Partial Quick output keeps the Quick artifact kind and completion reason.
It never writes `complete_report.md` or a decision state log. Generalize the
existing report helpers, discovery, brief API, list/detail views and downloads
to support that explicit kind. The existing manifest membership and content
hash checks still protect source-text access. Legacy reports without mode
metadata say "mode not recorded" rather than inventing it.

Add CLI `--mode` and `TRADINGAGENTS_RESEARCH_MODE`, interactive selection and
web settings, resolving explicit input, environment, remembered selection,
then Standard. An omitted mode in a legacy HTTP request still means Standard;
server-side preferences must not change it. Store validated preferences only.
Quick skips analyst/depth/deep-model prompts and irrelevant key checks. The
API validates the same policy as CLI/library callers. Freeze effective config
per queued item so later changes do not modify queued jobs or nested defaults.

For library users, add a mode dispatcher with a distinct Quick result carrying
the brief, completion and artifact references; no rating-shaped return value.
Keep `TradingAgentsGraph` and its `(final_state, rating)` API for Standard,
rejecting a non-Standard mode before SDK or memory initialization. An explicit
library config wins over environment defaults; a library call without mode
uses the already-resolved default config, whose built-in fallback is Standard.

Before Run show scope/exclusions, selected model/provider, deadline, unknown
ETA/cost and local-data gaps. During work distinguish queueing, acquisition,
generation, checks, stopping and provider failure. Keep mode visible after
completion and reload. Reports, exports and live metrics must suppress Quick
decision labels/rating badges. Show a Standard action as a separate new run.

First-slice Quick has **no checkpoint resume**. Stop retains a readable partial
artifact; trying again starts a new explicit run with a new budget and records
its relationship to the previous run. Quick neither reads nor writes Standard
checkpoints; inherited checkpoint preferences do not enable it. Explicit
resume requests for Quick are rejected. This keeps the one-generation-attempt
rule unambiguous, including SDK sends. Standard resume stays isolated by mode,
effective scope, model/prompt/policy versions, canonical ticker and date. A
changed deadline alone can resume compatible Standard work, matching C03's
existing signature rule. C04 stays per-run; a cross-run cache or incremental
reuse remains separately designed work.

### 6. Implementation sequence and acceptance

After the owner chooses scope and budget, implement test-first:

1. Mode policy/config validation, backward-compatible precedence, immutable
   queued config and backtest rejection. Existing Standard execution/prompt,
   analyst selection, round overrides and completion tests must pass unchanged.
2. Quick runner and bounded local/source work. Fixtures cover stale/as-of
   withholding, missing local financials, malicious source instructions, input
   truncation, invalid citations and numerical near misses. An instrumented
   blocking loader proves deadline/Stop terminate work without a late send,
   leaked worker or late worker artifact write, while coordinator-owned partial
   persistence remains possible. Count the at-most-one generation request,
   including SDK sends. A valid-citation response containing a rating, target
   or sizing instruction must be withheld, never rendered as the Quick brief.
3. Completion/artifact boundary. Direct library, CLI, web and queue tests
   inject decision fields into Quick state/checkpoints and prove no rating,
   memory settlement/write, decision log or Standard artifact can result.
   Cover no data, missing synthesis, provider failure, cancellation, deadline,
   queue removal and successful/with-gaps output.
4. Mode/scope/budget/provider/check data round-trips through submission, queue,
   trace, saved files, restart, report API, HTML/Markdown and source drawer.
   Verify pre-C06 preferences/reports, Standard deadline-only resume, Quick
   resume rejection and cross-mode checkpoint refusal. Assert no inherited
   retry/output/depth setting can increase Quick's fixed work caps.
5. Focused suites, planted boundary faults, full offline gate and a keyboard
   walkthrough in both themes at 1440 px and 375 px. Walkthrough checks the
   pre-run scope/deadline, unavailable Deep, unknown cost/ETA, sourced-fact
   rendering and persistent "no investment decision" labels.

Extend the existing C03, CLI precedence/headless/preferences, web job/queue,
checkpoint, reporting, C05 brief and source API tests. Offline fixtures prove
behavior and bounded work; they cannot establish live completion latency or
source quality. Record first useful **rendered** facts separately from a
generated or saved report; do not relabel existing telemetry milestones.

Any later live evaluation needs its own registered workload and allowance,
with cold/warm eligibility hashes, post-C03/C05 Standard configuration,
source/check coverage, failures/partials and actual usage. Use at least three
matched repetitions for diagnosis; do not qualify a p95 with fewer than 100
comparable runs. No speed, cost or supported-claim improvement is claimed here.

## Questions for the owner

1. **First Quick scope/checks.** (A, recommended) Local point-in-time facts,
   bounded price/news, one synthesis and code-only ID/figure checks, as above;
   qualitative support remains unchecked. (B) Source/fact packet only, with no
   generated synthesis. If B is selected, use a distinct source-only policy
   with zero model sends; sourced facts and deterministic checks are its
   required outputs, so absent synthesis does not make it partial. Adding
   source-entailment calls needs a separate policy and quality evaluation.
2. **Quick budget.** (A, recommended) 60 s active budget, no generation retry;
   partial output on a slow source/model. (B) 180 s active budget with a 120 s
   generation-attempt cap, still one attempt; initial 60 s completion objective
   is then explicitly deferred. Both retain separate queue-time measurement.
3. **Deep scope.** (A, recommended) Display Deep as planned/unavailable, build
   Quick/Standard first and keep full C06 acceptance open. (B) Expand C06 now
   to design and implement targeted retrieval plus an explicit Deep budget;
   do not substitute the existing five-round setting for that work.

## Integration note

On 20 October, GitHub marks PRs #3-#6 merged, but `origin/main` is `499fac9`
and lacks C05. PR #6 merged into `docs/c05-design` as `cc97989`, after that
branch's own PR had merged into `feat/c04-close-out`; the later C05 build did
not propagate to main. This design is based on `cc97989`, with its PR targeting
`docs/c05-design` so its diff contains only C06 documents. Reconcile the C05
branch into main before landing implementation. No earlier PR is still
awaiting its original merge, and no branch has been merged by this task.
