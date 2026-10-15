from decimal import Decimal

import pytest

from scripts.benchmark_jev_c01 import ProbeBudget


def test_budget_is_reserved_before_each_http_attempt():
    budget = ProbeBudget()
    for _ in range(6):
        budget.reserve()
    assert budget.attempts == 6
    assert budget.reserved == Decimal("0.016515072")
    with pytest.raises(RuntimeError):
        budget.reserve()
    assert budget.attempts == 6


def test_cap_blocks_send_even_when_no_usage_response_exists():
    budget = ProbeBudget("0.003")
    budget.reserve()
    with pytest.raises(RuntimeError):
        budget.reserve()
    assert budget.attempts == 1


@pytest.mark.parametrize("cap", ["0", "-1", "1.01", "NaN", "Infinity"])
def test_budget_rejects_limits_outside_authorization(cap):
    with pytest.raises(ValueError):
        ProbeBudget(cap)
