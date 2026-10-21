"""The post-screen Jev fallback under the run's Stop and deadline (docs/adr/0001).

Without the Jev extra but with a TypeSafe key, the Sentiment Analyst screens
social posts through ``post_screen``: raw HTTP on pool threads. Its sends and
retry waits must obey the run's control like every other model or Jev request.
"""
import threading
import time

import pytest

from tradingagents.agents import post_screen as typesafe
from tradingagents.run_control import DeadlineExceeded, RunCancelled, RunControl, use_control

QUESTIONS = {"is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"}}


class _Response:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


def _ok():
    # Answers both this file's question and the screen's own questions.
    answers = {name: {"type": spec["type"]} for name, spec in {**QUESTIONS, **typesafe.QUESTIONS}.items()}
    return _Response(200, {"model": "jev-1.13.0", "answers": answers})


class _Calls(list):
    queue: list
    sleeps: list
    after = None


@pytest.fixture
def post(monkeypatch):
    calls = _Calls()
    calls.queue, calls.sleeps = [], []

    def fake_post(url, **kwargs):
        calls.append(kwargs)
        item = calls.queue.pop(0) if calls.queue else _ok()
        if calls.after:
            calls.after()
        return item

    monkeypatch.setattr(typesafe.requests, "post", fake_post)
    monkeypatch.setattr(typesafe.time, "sleep", calls.sleeps.append)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    return calls


def _control(seconds_left=None):
    control = RunControl.from_config({}, cancel_event=threading.Event())
    if seconds_left is not None:
        control.deadline = time.monotonic() + seconds_left
    return control


def test_a_stopped_run_sends_no_screening_request_from_the_pool(post):
    control = _control()
    control.cancel()
    with use_control(control), pytest.raises(RunCancelled):
        typesafe._ask_each("Apple (AAPL)", ["first post", "second post"])
    assert post == []


def test_stop_during_a_retry_wait_ends_without_another_send(post):
    control = _control()
    post.queue.append(_Response(429, headers={"Retry-After": "5"}))
    post.after = control.cancel
    with use_control(control), pytest.raises(RunCancelled):
        typesafe.system_one({"post": "x"}, QUESTIONS)
    assert len(post) == 1
    assert post.sleeps == []


def test_a_retry_wait_past_the_deadline_is_not_taken(post):
    control = _control(seconds_left=3)
    post.queue.append(_Response(429, headers={"Retry-After": "10"}))
    with use_control(control), pytest.raises(DeadlineExceeded):
        typesafe.system_one({"post": "x"}, QUESTIONS)
    assert len(post) == 1
    assert post.sleeps == []


def test_each_attempt_timeout_is_cut_to_the_time_left(post):
    control = _control(seconds_left=4)
    with use_control(control):
        typesafe._ask_each("Apple (AAPL)", ["only post"])
    assert len(post) == 1
    assert 0 < post[0]["timeout"] <= 4


def test_outside_a_run_the_waits_and_timeout_are_unchanged(post):
    post.queue.append(_Response(529))
    typesafe.system_one({"post": "x"}, QUESTIONS)
    assert len(post) == 2
    assert len(post.sleeps) == 1
    assert post[0]["timeout"] == typesafe._TIMEOUT
