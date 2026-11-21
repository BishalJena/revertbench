"""Local operational quota windows; no automatic quota escalation or job framework."""

from revertbench.models.ledger import JsonlLedger


class QuotaExceeded(RuntimeError):
    pass


class RateScheduler:
    def __init__(
        self,
        ledger: JsonlLedger,
        model: str,
        *,
        rpm: int,
        tpm: int,
        rpd: int,
        tpd: int,
    ) -> None:
        if min(rpm, tpm, rpd, tpd) < 1:
            raise ValueError("Operational quotas must be positive")
        self.ledger, self.model = ledger, model
        self.rpm, self.tpm, self.rpd, self.tpd = rpm, tpm, rpd, tpd

    def delay(self, tokens: int, *, now: float) -> float:
        if tokens < 0 or tokens > min(self.tpm, self.tpd):
            raise QuotaExceeded("Request exceeds the configured operational token ceiling")
        delay = 0.0
        starts = [
            row
            for row in self.ledger.events
            if row["event"] == "quota_started" and row["model"] == self.model
        ]
        usage = {
            row["call_id"]: row["tokens"]
            for row in self.ledger.events
            if row["event"] == "quota_usage" and row["model"] == self.model
        }
        starts = [{**row, "tokens": usage.get(row["call_id"], row["tokens"])} for row in starts]
        for window, request_cap, token_cap in (
            (60, self.rpm, self.tpm),
            (86400, self.rpd, self.tpd),
        ):
            active = [row for row in starts if row["at"] + window > now]
            while active and (
                len(active) >= request_cap
                or sum(row["tokens"] for row in active) + tokens > token_cap
            ):
                earliest = min(active, key=lambda row: row["at"])
                delay = max(delay, earliest["at"] + window - now)
                active.remove(earliest)
        for row in self.ledger.events:
            if row["event"] == "quota_cooldown" and row["model"] == self.model:
                delay = max(delay, row["until"] - now)
        return max(delay, 0.0)

    def record_start(self, call_id: str, tokens: int, *, now: float) -> None:
        self.ledger.append(
            "quota_started", call_id=call_id, model=self.model, tokens=tokens, at=now
        )

    def record_usage(self, call_id: str, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("Reported token usage cannot be negative")
        for row in self.ledger.events:
            if (
                row["event"] == "quota_usage"
                and row["model"] == self.model
                and row["call_id"] == call_id
            ):
                if row["tokens"] != tokens:
                    raise QuotaExceeded("Recorded quota usage changed")
                return
        self.ledger.append("quota_usage", call_id=call_id, model=self.model, tokens=tokens)

    def cooldown(self, seconds: float, *, now: float) -> None:
        if not 0 <= seconds <= 86400:
            raise ValueError("Provider cooldown is outside supported bounds")
        self.ledger.append("quota_cooldown", model=self.model, until=now + seconds)
