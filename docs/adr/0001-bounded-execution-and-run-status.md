# ADR 0001: Bounded execution and truthful run status (C03)

Status: **Accepted** by the owner as proposed on 17 October 2026 (Asia/Calcutta):
a 1,200 s Standard deadline, a 180 s attempt timeout for every provider, partial
reports saved by default, and claim-check failures labelled only.
**Implemented** the same day and fixture tested; see
[Implementation](#implementation-17-october-2026). Task: C03 in the
[plan](../plan.md) and [progress tracker](../progress.md). Evidence class for
the proposal: source reviewed, plus offline inspection of the installed SDKs in
the WSL `.venv` (openai 3.24.0, anthropic 1.11.0, langchain-openai 1.6.7,
httpx2 2.13.1, typesafe-sdk). No provider was called.

## Context

| Finding | Evidence |
| --- | --- |
| The app sets **no per-request timeout** for OpenAI-compatible (including NVIDIA) or Anthropic chat models. LangChain passes `timeout=None`, which these SDKs read as "no timeout": connect, read, write and pool limits are all `None`. A stalled request can wait indefinitely. The Gemini client's comment that the other SDKs stop at 600 s does not hold through LangChain. C01 runs were bounded only because the runner passed 60 or 180 s explicitly | Offline inspection of a constructed client and request; [Google client](../../tradingagents/llm_clients/google_client.py) |
| Two retry layers multiply. Each of six outer attempts (waits 10, 20, 40, 60, 60 s = 190 s) contains an SDK call with 2 default retries, including on timeouts. SDK sleeps (0.5 s and 1 s, or a Retry-After of up to 120 s each) use uninterruptible `time.sleep`. Even with a 180 s attempt timeout, one logical call could take about 57 minutes (18 x 180 s attempts + 190 s outer waits + about 9 s of SDK backoff), longer with Retry-After. This is a source-derived bound, not an observation | [OpenAI-compatible client](../../tradingagents/llm_clients/openai_client.py); SDK `_base_client` |
| Structured output repeats provider failures. `invoke_structured` catches every exception, including transient errors left after all retries, then calls the free-text model, which runs the whole retry chain again. Affects the Research Manager, Trader, Portfolio Manager and Sentiment Analyst | [structured.py](../../tradingagents/agents/structured.py) |
| Stop ends the outer retry wait and the graph at its next step boundary. It does not stop SDK or Jev retry sleeps, or a request in flight; with no timeout, Stop can wait indefinitely | [jobs.py](../../cli/webui/jobs.py), existing `test_cancel_interrupts_a_provider_retry_wait` |
| A provider error ends a web run as `failed`. Finished analyst sections stay on the live page and the checkpoint allows a resume, but nothing partial is saved, and the failure is not attributed to a stage or provider | [jobs.py](../../cli/webui/jobs.py), [trading graph](../../tradingagents/graph/trading_graph.py) |
| `done` does not require every stage. The report writer drops empty sections; an empty final decision becomes REVIEW, yet the run still reads `done` | [reporting.py](../../tradingagents/reporting.py) |
| Some degradations are silent. A failed claim check returns the decision unchanged, which reads the same as a disabled check; failed debate judgments fall back without a label; Jev sentiment failure is explained only generically. The memory-log note is the one labelled degradation, and the pattern to follow | [claim check](../../tradingagents/agents/claim_check.py), [debate judgments](../../tradingagents/agents/debate_judgments.py), [memory step](../../tradingagents/graph/trading_graph.py) |
| Jev is already bounded: 10 s request timeout, 2 retries, 30 s retry-policy limit | Offline inspection of `TypeSafeClient` defaults |
| Backtests catch `Exception` per cell and continue the sweep | [backtest.py](../../tradingagents/backtest.py) |

C01 measured, on synthetic sources under the 180 s policy, research completions
of 285.3-494.7 s (n=6) and a longest successful NVIDIA response of 125.5 s; a
60 s limit would have cut off 11 of 206 successful responses
([benchmark record](../benchmarks/c01.md)).

## Decision

### 1. One run controller

A `RunControl` context variable replaces the bare `cancel_event`. It holds the
cancel event, a monotonic deadline, the mode's budget and the current retry
state. The web job, CLI, `propagate()`, each backtest cell and the benchmark
runner set it. LangGraph already copies the context into its worker threads.

### 2. Per-mode budgets

Configuration gains `run_budgets`, keyed by research mode. Only Standard exists
until C06.

| Budget | Standard (proposed) | Basis |
| --- | --- | --- |
| Run deadline: research start to persisted report | 1,200 s | About 2.4 x the slowest C01 completion; provisional until real-source runs are measured |
| Per-HTTP-attempt timeout | 180 s | The registered C01 recovery policy; covers the 125.5 s maximum |
| Retries per logical model call | 5, waiting 10, 20, 40, 60, 60 s | Today's outer schedule, now the only retry layer |
| Retry-After honoured | Up to 120 s, never past the deadline | Matches the SDK cap |

Queue time is not charged to the deadline; it remains a separate measured
interval (queue budgets belong to C12). C06 sets Quick from measured runs
(the plan's target is 60 s p95). Deep takes a user-selected budget, shown
before the run starts and never silently exceeded.

### 3. One retry owner

Under a `RunControl`, OpenAI-compatible clients get SDK `max_retries=0`, and the
existing outer loop becomes the only retry layer. It retries 5xx, 429,
connection errors and timeouts. `llm_max_retries`, when set, caps that loop's
retry count. Before each wait the loop stops on cancellation. It raises
`DeadlineExceeded`, chained to the provider error, when the wait would leave
less than a minimum useful attempt (proposed 30 s) before the deadline. Waits
stay interruptible.

### 4. A guard on every HTTP attempt

The httpx/httpx2 request hooks C01 installed on OpenAI, Anthropic and Jev
clients gain two duties. They refuse a send after Stop or the deadline,
recording it as `not_sent` as the C01 guard did. They also clamp the request's
timeout to the smaller of the attempt timeout and the time left. SDK-internal
and Jev retries pass through the same hook, so no layer can send after the
deadline. Google and Bedrock transports are not hooked. They rely on
step-boundary checks and a 180 s client timeout where the SDK accepts one, and
the trace records that gap.

Limitation: httpx read timeouts measure the gap between bytes. A server that
trickles bytes can outlast the clamp. The run then ends with
`deadline_exceeded` and the overrun is recorded, not hidden.

### 5. Cancellation

Stop or an expired deadline sets the controller's event. Retry waits end at
once, no layer sends again, and the graph stops at its next step. A request
already in flight is not aborted, because synchronous httpx offers no safe
cross-thread abort. Its clamped timeout bounds the delay to at most 180 s, and
the page says "Stopping: waiting for N requests in flight".

`RunCancelled` and `DeadlineExceeded` derive from a `RunInterrupted`
`BaseException`, as `asyncio.CancelledError` does. The existing broad
`except Exception` fallbacks (claim check, Jev sentiment, debate judgments,
structured output) therefore cannot swallow a stop. Tests verify this through
the real LangGraph runner. If LangGraph mishandles it, those handlers re-raise
explicitly instead. Backtests record `DeadlineExceeded` as a failed cell and
continue; `RunCancelled` ends the sweep. `invoke_structured` falls back to free
text only on parsing, binding or format failures, not on provider errors.

### 6. Truthful status and retained partial work

The lifecycle status (`pending`, `running`, `done`, `failed`, `cancelled`) stays
for compatibility. Each run also gets a completion record in the job snapshot,
the API, the trace and saved artifacts:

| Field | Values |
| --- | --- |
| `state` | `complete`, `complete_with_gaps`, `partial`, `none` |
| `stop_reason` | none, `cancelled`, `deadline`, `provider_error`, `error`; `missing_output` added in the build |
| `stages` | each required stage: `done`, `missing` or `reused` (from a checkpoint) |
| `checks` | claim check, Jev sentiment, debate judgments, memory: `ran`, `failed`, `disabled` or `unavailable` |
| `provider_failure` | stage, provider, model, error class (timeout, rate limit, server, connection, auth/quota), attempts and seconds waited; no messages or bodies |

Rules:

- `done` requires a non-empty output from every stage the selected analysts
  need, and no interruption. A failed or unavailable check gives
  `complete_with_gaps`, with the gap named in the report header and the UI.
- An interrupted run saves `partial_report.md`, `partial_report.html` and
  `run_status.json` in a folder ending `_partial`. The first line is a banner:
  incomplete research, the reason and the stage, no decision or rating.
- An interrupted run never writes `complete_report.md`, a
  `full_states_log_*.json` (the 129BOB importer reads those), a memory-log
  decision or a rating. Its checkpoint is kept.
- The run page keeps sections from failed and stopped runs, labelled as
  produced before the stop, and shows no rating plate. A provider failure
  names the stage, provider, error class and attempts. Retry state and the
  time left before the deadline appear while the run is active. The
  saved-report list labels partial reports.

### 7. Restart

Running the same ticker, date and settings again resumes from the checkpoint,
as it does now. The budget keys join `llm_max_retries` outside the checkpoint
signature, so a resume can use a longer deadline. A resumed run marks reused
stages as `reused`.

## Offline acceptance tests

No live provider calls. Tests use fake clocks, scripted models and httpx mock
transports.

1. A retry is skipped, and `DeadlineExceeded` raised, when its wait would cross
   the deadline; the provider error is chained.
2. With 5xx responses from a mock transport, total HTTP attempts and elapsed
   (fake) time stay within the budget, with SDK retries off.
3. After Stop or the deadline, the guard refuses OpenAI, Anthropic and Jev
   sends before transmission; the mock transport receives nothing.
4. Each request's timeout equals min(attempt timeout, time left).
5. Stop during a wait and during an in-flight request: no later send, status
   `cancelled`, stop latency within the attempt timeout.
6. Claim check, Jev sentiment, debate judgments, structured output and the
   backtest loop do not swallow a stop; structured output does not repeat an
   exhausted provider error as free text.
7. A provider failure in Bear Researcher on the real graph with a scripted
   model: not `done`; analyst sections retained; a partial report saved; no
   complete report, state log, memory entry or rating.
8. An empty required stage cannot finish as `done`.
9. A claim-check failure is labelled in the report header and in the API.
10. Stop after the analysts, then resubmit: analyst model calls are not
    repeated, and the run completes; a changed deadline still resumes.
11. Traces record the stop reason and deadline without credentials or content.

Then the existing regression gates in the plan and the full non-integration
suite run in WSL.

## Not in C03

Quick and Deep budget values (C06); queue budgets and cross-run quotas (C12);
spending budgets; per-source evidence coverage (C04/C05); aborting in-flight
sockets; Google and Bedrock transport hooks; and any change to how an
unchecked decision is rated. Live verification needs a new allowance recorded
by the owner.

## Questions for the owner

1. Is 1,200 s an acceptable provisional Standard deadline? 900 s would be
   about 1.8 x the slowest C01 run.
2. Should the 180 s attempt timeout become the default for every provider,
   replacing today's no-timeout default?
3. Should interrupted runs save partial reports to disk by default?
4. Should a decision whose claim check failed to run be labelled only
   (proposed), or sent to REVIEW?

## Implementation order after approval

1. `RunControl`, interruption exceptions, budget configuration and checkpoint
   signature exclusion.
2. Single retry owner with deadline-aware waits.
3. Transport guard: refusal and timeout clamping.
4. Structured-output fallback and broad handlers.
5. Completion record, required-stage gate and check labels.
6. Partial artifacts, web job/API/UI labels and the saved-report list.
7. Restart tests, the full suite, and the progress update.

## Implementation, 17 October 2026

Built in that order, offline only. The pieces:

| Piece | Where |
| --- | --- |
| `RunBudget`, `RunControl`, `RunInterrupted` (`RunCancelled`, `DeadlineExceeded`), check recording | [run_control.py](../../tradingagents/run_control.py) |
| `research_mode`, `run_budgets`, `save_partial_reports` | [default_config.py](../../tradingagents/default_config.py) |
| 180 s timeout passed to every provider client | [factory.py](../../tradingagents/llm_clients/factory.py) |
| Single retry loop, SDK `max_retries=0`, Retry-After | [openai_client.py](../../tradingagents/llm_clients/openai_client.py) |
| HTTP guard and timeout clamp; check before each generation; requests in flight | [llm_clients/telemetry.py](../../tradingagents/llm_clients/telemetry.py) |
| Provider-error classes without message text | [llm_clients/errors.py](../../tradingagents/llm_clients/errors.py) |
| Completion record, required stages, stop and gap wording | [completion.py](../../tradingagents/completion.py) |
| Partial report tree, run-status header and `run_status.json` | [reporting.py](../../tradingagents/reporting.py), [report_html.py](../../tradingagents/report_html.py) |
| Web job, API, page and saved-report list | [jobs.py](../../cli/webui/jobs.py), [server.py](../../cli/webui/server.py), [app.js](../../cli/webui/static/app.js) |
| CLI, `propagate()` and backtests | [run.py](../../cli/run.py), [trading_graph.py](../../tradingagents/graph/trading_graph.py), [backtest.py](../../tradingagents/backtest.py) |

Where the build differs from, or adds to, the proposal:

- A graph that reaches its end with a required stage empty stops with
  `missing_output`. No decision is recorded, and its checkpoint is cleared so
  the next run starts fresh rather than resuming the same empty end. A debate
  turn that holds only its speaker label ("Bear Analyst:") counts as empty.
- A partial report keeps every section the run finished, including any
  Portfolio Manager text, but has no rating line, no rating in the HTML title
  and a banner saying it is not a decision to act on.
- For OpenAI-compatible providers, `llm_max_retries` now caps the single retry
  loop (0 turns retries off). For Anthropic, Google, Azure and Bedrock it is
  still the SDK's own count. Bedrock receives no timeout argument and keeps
  botocore's own limits; Google and Bedrock rely on the check before each
  generation, as proposed.
- Without Jev set up, every complete run is `complete_with_gaps`: the claim
  check and Jev judgments are `unavailable`, which the approved rule counts as
  gaps. `jev_enabled: False` records them as `disabled`, which is not a gap.
- In the CLI, Ctrl-C acts as Stop: requests in flight send nothing more, the
  partial report is saved and the exit code is 130. An error that is not a
  provider failure, Stop or deadline is raised as before, after the partial
  report is saved.
- `propagate()` raises `RunInterrupted` or `IncompleteResearch`; the
  checkpoint keeps what it finished. It saves no report, as before.
- The C01 benchmark runner hashes the whole configuration, so the three new
  keys give post-C03 runs a new configuration hash. They are a new cohort, and
  the runner's warm-seed check refuses to pair them with C01 caches. The
  runner's `llm_max_retries=0` now turns off the outer retries as well; in
  C01 its guard already stopped all sends at the first failure, so completed
  C01 runs made no retries either way.

Known limits, unchanged from the proposal unless noted: a request in flight is
not aborted; HTTPX read timeouts bound the gap between bytes; Jev's own retry
sleeps (at most 5 s) cannot be interrupted, though its sends are refused; the
Google and Bedrock transports are not hooked. Found during the build and not
fixed here: `AnthropicClient` forwards `http_client`, which langchain-anthropic
moves into `model_kwargs` instead of using it.

Verification is recorded in the [progress tracker](../progress.md).
