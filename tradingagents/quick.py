"""A bounded research brief, deliberately separate from a decision graph (ADR 0004)."""
from __future__ import annotations

import hashlib
import html
import json
import multiprocessing
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from tradingagents.modes import mode_config, mode_view
from tradingagents.run_control import DeadlineExceeded, RunCancelled, RunControl, RunInterrupted


def _worker(send, target, args):
    try:
        def emit(event):
            if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) > 128000:
                raise ValueError("Quick source event exceeds its transfer limit.")
            send.send(("event", event))

        result = target(emit, *args)
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 128000:
            raise ValueError("Quick worker result exceeds its transfer limit.")
        send.send(("result", result))
    except BaseException as exc:
        # Never send provider error bodies/credentials to the coordinator or browser.
        send.send(("error", type(exc).__name__))
    finally:
        send.close()


def bounded_call(target, args, *, timeout, cancel, on_event=None):
    """A real process boundary; Stop/timeout kills all its source/SDK threads."""
    if cancel.is_set():
        raise RunCancelled("Quick research was stopped.")
    if timeout <= 0:
        raise TimeoutError("No phase time remains.")
    ctx = multiprocessing.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_worker, args=(send, target, args), daemon=True)
    end = time.monotonic() + timeout
    try:
        process.start()
        send.close()
        while True:
            if cancel.is_set():
                raise RunCancelled("Quick research was stopped.")
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Quick phase reached its time limit.")
            if receive.poll(min(remaining, 0.05)):
                try:
                    kind, value = receive.recv()
                except EOFError:
                    raise RuntimeError("Quick worker exited without a result.") from None
                if kind == "event" and on_event:
                    on_event(value)
                elif kind == "result":
                    return value
                elif kind == "error":
                    raise RuntimeError(f"Quick worker failed ({value}).")
            elif not process.is_alive():
                raise RuntimeError("Quick worker exited without a result.")
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
            if process.is_alive():
                raise RuntimeError("Quick worker could not be stopped.")
            process.close()
        receive.close()
        send.close()


_PROPOSAL = re.compile(
    r"\b(?:buy|sell|hold|overweight|underweight|bullish|bearish)\s+(?:rating|recommendation|this\s+stock|the\s+stock)"
    r"|\b(?:rating\s*[:=]|price\s+target|target\s+price|stop[ -]?loss|position\s+siz|entry\s+(?:price|point))"
    r"|\b(?:allocate|invest|purchase|short|sell|buy)\b.{0,50}(?:%|shares|portfolio|stock|position)"
    r"|\b(?:recommend(?:ation|ed|ing)?|action|decision|verdict)\s*[:=]"
    r"|\b(?:recommend|suggest|consider)\s+(?:buying|selling|holding|shorting|investing)\b"
    r"|\b(?:buy|sell|hold|overweight|underweight|bullish|bearish)\b"
    r"|\b(?:rating|recommendation|action|decision|verdict)\b.{0,35}\breview\b"
    r"|^\W*(?:(?:strong|moderate|weak)\s+)?(?:buy|sell|hold|review|overweight|underweight|bullish|bearish)\W*$",
    re.I | re.M)


def validate_summary(summary, sources: dict[str, str]) -> dict:
    """Check IDs and figure presence; this is not a source-entailment judgment."""
    from tradingagents.agents.claim_check import figure_stated, figures

    result = {"valid": False, "observations": [], "gaps": [], "qualitative_support": "not_checked"}
    if not isinstance(summary, dict) or set(summary) != {"observations", "gaps", "questions"}:
        return {**result, "reason": "The synthesis did not match the brief schema."}
    observations = summary["observations"]
    if not isinstance(observations, list) or not 1 <= len(observations) <= 8:
        return {**result, "reason": "The synthesis has no valid observations."}
    for field in ("gaps", "questions"):
        values = summary[field]
        if not isinstance(values, list) or len(values) > 8 or any(
            not isinstance(v, str) or len(v) > 500 or _PROPOSAL.search(v) for v in values
        ):
            return {**result, "reason": "The synthesis contains out-of-scope content."}
    checked = []
    gaps = list(summary["gaps"])
    for item in observations:
        if not isinstance(item, dict) or set(item) != {"text", "evidence_ids"}:
            return {**result, "reason": "The synthesis contains an unexpected field."}
        text, ids = item["text"], item["evidence_ids"]
        if (not isinstance(text, str) or not text.strip() or len(text) > 1500 or _PROPOSAL.search(text)
                or not isinstance(ids, list) or not ids or len(ids) > 8
                or any(not isinstance(i, str) or i not in sources for i in ids)):
            return {**result, "reason": "The synthesis contains an invalid citation or trade proposal."}
        traces = []
        for fig in figures(text):
            found = [{"id": key, "line": line[:240]}
                     for key in dict.fromkeys(ids) for line in sources[key].splitlines()
                     if figure_stated(fig, figures(line, claim=False))][:5]
            traces.append({"figure": fig.written, "found": found})
            if not found:
                gaps.append(f"Figure {fig.written} was not found in the cited sources.")
        checked.append({**item, "figures": traces})
    return {**result, "valid": True, "observations": checked, "gaps": gaps, "reason": None}


@dataclass
class QuickResult:
    state: dict
    completion: dict
    path: Path | None
    settings: dict


def quick_markdown(state: dict, completion: dict, settings: dict) -> str:
    from tradingagents.reporting import sources_markdown

    text = (f"# Quick research: {state['company_of_interest']}\n\n"
            f"- Analysis date: {state['trade_date']}\n- Generated: {state['generated']}\n"
            f"- Mode: Quick ({settings['policy']})\n- State: {completion['state']}\n"
            "- No investment decision produced; no rating.\n"
            f"- Model: {settings['provider']} / {settings['model']}\n"
            "- Active budget: 60 seconds; ETA and cost unavailable.\n\n")
    text += "## Sourced facts and headlines\n\n"
    for e in state.get("quick_sources", []):
        text += f"### {e['id']}: {e['tool']} ({e['coverage']})\n\n{e['text']}\n\n"
    text += "## Observations\n\n"
    checks = state.get("quick_checks") or {}
    for item in checks.get("observations", []):
        text += f"- {item['text']} [{', '.join(item['evidence_ids'])}]\n"
    if not state.get("quick_summary"):
        text += "Synthesis not available. " + (completion.get("detail") or "") + "\n"
    text += "\nQualitative source support is not checked. Figure presence does not prove support.\n\n"
    text += "## Gaps and research questions\n\n"
    for gap in state.get("quick_gaps", []):
        text += f"- {gap}\n"
    for question in (state.get("quick_summary") or {}).get("questions", []):
        text += f"- Research question: {question}\n"
    text += "\n" + sources_markdown(state.get("quick_sources", [])) + "\n"
    return text


def _save(state, completion, config, settings, *, html_output=True) -> Path:
    from tradingagents.dataflows.provenance import store_dir
    from tradingagents.reporting import _write_evidence

    folder = Path(config["results_dir"]) / "reports" / f"{state['company_of_interest']}_{state['run_id']}_quick"
    folder.mkdir(parents=True, exist_ok=False)
    _write_evidence(state, folder, store_dir(config))
    (folder / "run_status.json").write_text(json.dumps(completion, indent=2), encoding="utf-8")
    (folder / "quick_data.json").write_text(json.dumps({"schema": 1, "state": state, "settings": settings},
                                                      indent=2, ensure_ascii=False), encoding="utf-8")
    text = quick_markdown(state, completion, settings)
    (folder / "quick_brief.md").write_text(text, encoding="utf-8")
    if html_output:
        (folder / "quick_brief.html").write_text(
            '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>Quick research</title>'
            '<style>body{max-width:900px;margin:3rem auto;padding:0 1rem;font:16px/1.6 system-ui}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><main><pre>'
            + html.escape(text) + '</pre></main></html>', encoding="utf-8")
    return folder / "quick_brief.md"


def run_quick(ticker: str, trade_date: str, *, config=None, cancel=None, control=None,
              on_event=None, phase_runner=bounded_call, resume=False, save=True, html=True,
              asset_type="stock") -> QuickResult:
    """Run one Quick attempt. Only the coordinator stores report/source artifacts."""
    from tradingagents.quick_sources import acquire, synthesize

    if resume:
        raise ValueError("Quick does not support checkpoint resume; start a new run.")
    if asset_type not in ("stock", "crypto"):
        raise ValueError("Quick asset_type must be stock or crypto.")
    if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z0-9_^=.-]{1,80}", ticker) or ticker in (".", ".."):
        raise ValueError("Invalid ticker.")
    day = date.fromisoformat(trade_date)
    if day.isoformat() != trade_date or day > date.today():
        raise ValueError("Analysis date must be canonical and not in the future.")
    config = mode_config({**(config or {}), "research_mode": "quick", "asset_type": asset_type})
    cancel = cancel if cancel is not None else threading.Event()
    control = control if control is not None else RunControl.from_config(config, cancel_event=cancel)
    cancel = control.cancel_event
    # A caller-supplied Standard/unbounded control cannot enlarge Quick's policy.
    run_deadline = min(control.deadline or float("inf"), time.monotonic() + 60)
    work_cutoff = run_deadline - 5
    settings = mode_view(config)
    state = {"research_mode": "quick", "decision_eligible": False, "rating": None,
             "company_of_interest": ticker.upper(), "trade_date": trade_date, "asset_type": asset_type,
             "run_id": uuid4().hex, "generated": datetime.now(UTC).isoformat(),
             "quick_summary": None, "quick_checks": {}, "quick_sources": [], "evidence": {},
             "quick_gaps": [], "settings": settings, "usage": None, "cost": None,
             "generation_attempts": 0, "generation_sends": None,
             "transport_coverage": "worker sends unobserved", "source_usage": None}
    reason, detail = None, None

    def event(value):
        if cancel.is_set():
            raise RunCancelled("Quick research was stopped.")
        if value.get("type") == "evidence":
            from tradingagents.dataflows.provenance import store_dir, store_text

            if len(state["quick_sources"]) >= 3:
                raise ValueError("Quick received more than its three registered sources.")
            text = value["text"]
            if len(text) > 24000:
                text = text[:23940] + "\n[Evidence truncated to the Quick source budget.]"
            digest = store_text(text, store_dir(config)) if save else hashlib.sha256(text.encode("utf-8")).hexdigest()
            seq = len(state["quick_sources"])
            e = {k: value[k] for k in ("tool", "arguments", "coverage", "acquisitions")}
            e.update({"id": f"E{seq + 1}", "analyst": "Quick research", "seq": seq,
                      "content_sha256": digest, "stored": bool(save), "chars": len(text)})
            state["evidence"][f"Quick research#{seq:03}"] = e
            state["quick_sources"].append({**e, "text": text})
        elif value.get("type") == "usage":
            state["usage"] = value.get("usage") or None
        if on_event:
            on_event(value)

    try:
        control.check()
        if on_event:
            on_event({"type": "stage", "stage": "Acquisition"})
        try:
            phase_runner(acquire, (config, ticker.upper(), trade_date),
                         timeout=min(15, work_cutoff - time.monotonic()), cancel=cancel, on_event=event)
        except (TimeoutError, RuntimeError) as exc:
            state["quick_gaps"].append(str(exc))
        control.check()
        sources = {e["id"]: e["text"] for e in state["quick_sources"] if e["coverage"] == "available"}
        if not sources:
            raise ValueError("No usable evidence was obtained; no synthesis was requested.")
        # All source labels/dates count toward the single bounded prompt packet.
        packet, sent_sources, room = [], {}, 24000
        for key, text in sources.items():
            part = f"[{key}]\n{text}"
            omitted = "\n[Evidence truncated to the Quick input budget.]"
            if len(part) > room:
                part = part[:max(0, room - len(omitted))] + omitted if room >= len(omitted) else ""
            if part:
                packet.append(part)
                sent_sources[key] = part.split("\n", 1)[-1]
                room -= len(part)
            if room <= 0:
                break
        if on_event:
            on_event({"type": "stage", "stage": "Synthesis"})
        if work_cutoff <= time.monotonic():
            raise DeadlineExceeded("The Quick work budget is exhausted.")
        state["generation_attempts"] = 1
        response = phase_runner(synthesize, (config, ticker.upper(), trade_date, "".join(packet)),
                                timeout=min(40, work_cutoff - time.monotonic()), cancel=cancel, on_event=event)
        control.check()
        state["usage"] = response.get("usage") or state["usage"]
        state["output_cap"] = response.get("output_cap", "requested; provider enforcement not independently verified")
        if on_event:
            on_event({"type": "stage", "stage": "Checks"})
        control.check()
        checks = validate_summary(response.get("summary"), sent_sources)
        state["quick_checks"] = checks
        if not checks["valid"]:
            raise ValueError(checks["reason"])
        state["quick_summary"] = response["summary"]
        state["quick_gaps"].extend(checks["gaps"])
    except RunInterrupted as exc:
        reason, detail = exc.reason, str(exc)
    except KeyboardInterrupt:
        cancel.set()
        reason, detail = "cancelled", "Quick research was stopped."
    except (Exception, TimeoutError) as exc:
        reason = "cancelled" if cancel.is_set() else "deadline" if isinstance(exc, TimeoutError) else "error"
        detail = str(exc)
    if any(e["coverage"] != "available" for e in state["quick_sources"]):
        state["quick_gaps"].append("Some requested sources were unavailable or withheld.")
    state["quick_gaps"].append("Qualitative source support not checked; full decision checks are outside Quick scope.")
    if detail:
        state["quick_gaps"].append(detail)
    usable = any(e["coverage"] == "available" for e in state["quick_sources"])
    complete = state["quick_summary"] is not None and reason is None
    completion = {
        "schema": 1, "mode": "quick", "output_kind": "quick", "decision_eligible": False,
        "state": ("complete_with_gaps" if state["quick_gaps"] else "complete") if complete else "partial" if usable else "none",
        "stop_reason": reason, "detail": detail, "deadline_seconds": 60,
        "stages": {"Acquisition": "done" if usable else "missing", "Synthesis": "done" if complete else "missing"},
        "missing": [] if complete else ["Synthesis"], "gaps": list(state["quick_gaps"]),
        "checks": {"source_support": "not_in_scope"},
        "provider_failure": None,
    }
    path = _save(state, completion, config, settings, html_output=html) if save else None
    return QuickResult(state, completion, path, settings)
