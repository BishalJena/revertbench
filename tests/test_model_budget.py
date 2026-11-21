from decimal import Decimal
from pathlib import Path

import pytest

from revertbench.models.budget import BudgetExceeded, CostBudget
from revertbench.models.ledger import JsonlLedger


def test_budget_reserves_before_spend_and_settles_once(tmp_path: Path) -> None:
    with JsonlLedger(tmp_path / "spend.jsonl") as ledger:
        budget = CostBudget(ledger, "experiment", Decimal("0.25"))
        budget.reserve("one", Decimal("0.20"))
        with pytest.raises(BudgetExceeded):
            budget.reserve("two", Decimal("0.06"))
        budget.settle("one", Decimal("0.01"))
        budget.settle("one", Decimal("0.01"))
        budget.reserve("two", Decimal("0.20"))
        assert budget.totals() == {"settled": Decimal("0.01"), "reserved": Decimal("0.20")}


def test_uncertain_usage_keeps_full_reservation_across_resume(tmp_path: Path) -> None:
    path = tmp_path / "spend.jsonl"
    with JsonlLedger(path) as ledger:
        budget = CostBudget(ledger, "experiment", Decimal("0.25"))
        budget.reserve("one", Decimal("0.20"))
        budget.settle("one", None)
    with JsonlLedger(path) as ledger:
        budget = CostBudget(ledger, "experiment", Decimal("0.25"))
        assert budget.totals()["reserved"] == Decimal("0.20")
        with pytest.raises(BudgetExceeded):
            budget.reserve("retry-of-one", Decimal("0.06"))


def test_project_cap_is_shared_between_experiments(tmp_path: Path) -> None:
    with JsonlLedger(tmp_path / "spend.jsonl") as ledger:
        first = CostBudget(ledger, "first", Decimal("5"))
        first.reserve("first-call", Decimal("4.90"))
        first.settle("first-call", Decimal("4.90"))
        second = CostBudget(ledger, "second", Decimal("0.25"))
        with pytest.raises(BudgetExceeded):
            second.reserve("second-call", Decimal("0.11"))


def test_actual_usage_over_reservation_records_then_stops(tmp_path: Path) -> None:
    with JsonlLedger(tmp_path / "spend.jsonl") as ledger:
        budget = CostBudget(ledger, "experiment", Decimal("0.25"))
        budget.reserve("one", Decimal("0.01"))
        with pytest.raises(BudgetExceeded):
            budget.settle("one", Decimal("0.02"))
        assert budget.totals()["settled"] == Decimal("0.02")


def test_prior_overrun_stops_new_requests_after_process_resume(tmp_path: Path) -> None:
    path = tmp_path / "spend.jsonl"
    with JsonlLedger(path) as ledger:
        budget = CostBudget(ledger, "experiment", Decimal("0.25"))
        budget.reserve("one", Decimal("0.01"))
        with pytest.raises(BudgetExceeded):
            budget.settle("one", Decimal("0.02"))
    with JsonlLedger(path) as ledger:
        budget = CostBudget(ledger, "new-experiment", Decimal("0.25"))
        with pytest.raises(BudgetExceeded):
            budget.reserve("two", Decimal("0.01"))
