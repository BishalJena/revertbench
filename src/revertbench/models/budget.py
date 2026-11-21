"""Decimal reservations shared across experiments; unknown usage stays reserved."""

from decimal import Decimal

from revertbench.models.ledger import JsonlLedger


class BudgetExceeded(RuntimeError):
    pass


class CostBudget:
    def __init__(self, ledger: JsonlLedger, experiment_id: str, limit: Decimal) -> None:
        if not limit.is_finite() or not Decimal(0) < limit <= Decimal(5):
            raise ValueError("Experiment budget must be positive and at most USD5")
        self.ledger, self.experiment_id, self.limit = ledger, experiment_id, limit

    def _entries(self) -> dict[str, tuple[str, Decimal, Decimal | None]]:
        result: dict[str, tuple[str, Decimal, Decimal | None]] = {}
        for row in self.ledger.events:
            if row["event"] == "budget_reserved":
                result[row["key"]] = (row["experiment_id"], Decimal(row["amount"]), None)
            elif row["event"] == "budget_settled":
                experiment, reserved, _ = result[row["key"]]
                result[row["key"]] = (experiment, reserved, Decimal(row["amount"]))
        return result

    def totals(self, *, project: bool = False) -> dict[str, Decimal]:
        entries = [
            entry for entry in self._entries().values() if project or entry[0] == self.experiment_id
        ]
        return {
            "settled": sum((actual for _, _, actual in entries if actual is not None), Decimal(0)),
            "reserved": sum(
                (reserved for _, reserved, actual in entries if actual is None), Decimal(0)
            ),
        }

    def reserve(self, key: str, amount: Decimal) -> None:
        if not amount.is_finite() or amount < 0:
            raise ValueError("Reservation must be a finite nonnegative amount")
        if any(
            actual is not None and actual > reserved
            for _, reserved, actual in self._entries().values()
        ):
            raise BudgetExceeded("Prior usage exceeded its reservation; reconciliation required")
        previous = self._entries().get(key)
        if previous is not None:
            if previous[:2] != (self.experiment_id, amount):
                raise ValueError("Existing reservation identity changed")
            return
        for project, limit in ((False, self.limit), (True, Decimal(5))):
            totals = self.totals(project=project)
            if totals["settled"] + totals["reserved"] + amount > limit:
                raise BudgetExceeded("Request would exceed the recorded spending cap")
        self.ledger.append(
            "budget_reserved", key=key, experiment_id=self.experiment_id, amount=str(amount)
        )

    def settle(self, key: str, amount: Decimal | None) -> None:
        previous = self._entries().get(key)
        if previous is None:
            raise ValueError("Cannot settle an unreserved request")
        if amount is None:
            self.ledger.append("budget_usage_unknown", key=key, experiment_id=self.experiment_id)
            return
        if not amount.is_finite() or amount < 0:
            raise ValueError("Actual cost must be finite and nonnegative")
        if previous[2] is not None:
            if previous[2] != amount:
                raise ValueError("Recorded usage changed")
            return
        self.ledger.append(
            "budget_settled", key=key, experiment_id=self.experiment_id, amount=str(amount)
        )
        if amount > previous[1]:
            raise BudgetExceeded("Reported usage exceeded its reservation; stop further calls")
