from pathlib import Path

import pytest

from revertbench.models.ledger import JsonlLedger
from revertbench.models.scheduler import QuotaExceeded, RateScheduler


def test_rate_windows_and_cooldown_survive_resume(tmp_path: Path) -> None:
    path = tmp_path / "quota.jsonl"
    with JsonlLedger(path) as ledger:
        scheduler = RateScheduler(ledger, "model", rpm=1, tpm=100, rpd=10, tpd=1000)
        assert scheduler.delay(40, now=0) == 0
        scheduler.record_start("one", 40, now=0)
        assert scheduler.delay(40, now=10) == 50
        scheduler.cooldown(90, now=10)
    with JsonlLedger(path) as ledger:
        scheduler = RateScheduler(ledger, "model", rpm=1, tpm=100, rpd=10, tpd=1000)
        assert scheduler.delay(40, now=61) == 39
        assert scheduler.delay(40, now=100) == 0


def test_request_over_operational_token_limit_never_enters_queue(tmp_path: Path) -> None:
    with JsonlLedger(tmp_path / "quota.jsonl") as ledger:
        scheduler = RateScheduler(ledger, "model", rpm=1, tpm=100, rpd=10, tpd=1000)
        with pytest.raises(QuotaExceeded):
            scheduler.delay(101, now=0)


def test_usage_refines_reserved_tokens_and_cannot_be_changed_on_resume(tmp_path: Path) -> None:
    with JsonlLedger(tmp_path / "quota.jsonl") as ledger:
        scheduler = RateScheduler(ledger, "model", rpm=10, tpm=100, rpd=100, tpd=1000)
        scheduler.record_start("one", 90, now=0)
        assert scheduler.delay(20, now=1) == 59
        scheduler.record_usage("one", 10)
        assert scheduler.delay(20, now=1) == 0
        scheduler.record_usage("one", 10)
        with pytest.raises(QuotaExceeded):
            scheduler.record_usage("one", 11)
