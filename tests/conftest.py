import pytest

import extract


@pytest.fixture(autouse=True)
def private_cost_log(tmp_path, monkeypatch):
    """Keep every test from writing into the real cost log."""
    monkeypatch.setattr(extract, "COST_LOG", tmp_path / "cost_log.jsonl")
    monkeypatch.setattr(extract, "RATE_LIMIT_WAITS", ())  # tests never sleep; the ones about waiting set their own
    monkeypatch.setattr(extract, "SECONDS_BETWEEN_CALLS", 0.0)
    monkeypatch.setattr(extract, "_last_call", 0.0)
