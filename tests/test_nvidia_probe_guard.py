import pytest

from scripts.benchmark_nvidia_c01 import AttemptGuard, main, numerical_quota_headers


@pytest.mark.unit
def test_nvidia_guard_reserves_before_each_send_and_blocks_seventh():
    guard = AttemptGuard()
    assert [guard.reserve() for _ in range(6)] == list(range(1, 7))
    with pytest.raises(RuntimeError, match="allowance"):
        guard.reserve()
    assert guard.attempts == 6


@pytest.mark.unit
@pytest.mark.parametrize("maximum", [0, 7, -1, True, 1.5])
def test_nvidia_guard_rejects_wider_or_invalid_allowance(maximum):
    with pytest.raises(ValueError):
        AttemptGuard(maximum)


@pytest.mark.unit
def test_quota_headers_exclude_non_numerical_values_and_credentials():
    assert numerical_quota_headers({
        "X-RateLimit-Remaining-Requests": " 3 ",
        "x-ratelimit-limit-tokens": "12345",
        "x-ratelimit-reset-tokens": "2m",
        "x-ratelimit-unknown": "NaN",
        "authorization": "Bearer secret",
        "x-request-id": "123",
    }) == {"x-ratelimit-remaining-requests": 3, "x-ratelimit-limit-tokens": 12345}


@pytest.mark.unit
def test_replay_requires_execute_before_loading_credentials_or_creating_clients():
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2
