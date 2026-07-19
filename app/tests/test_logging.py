"""Operational logs must expose outcomes without exposing student content."""

import logging
from pathlib import Path

import pytest
from mali.actions import Actor, StartPlacement
from mali.ids import learner_id

from mali_app.demo import demo_curriculum
from mali_app.store import SQLiteRecordStore
from mali_app.store_types import ExecutionStatus


def test_record_logs_commit_and_refusal_without_display_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    learner = learner_id("logging-learner")
    store = SQLiteRecordStore(str(tmp_path / "logging.db"), demo_curriculum())

    with caplog.at_level(logging.DEBUG, logger="mali_app.store"):
        store.register(learner, "Private student name")
        committed = store.execute(learner, StartPlacement(), Actor.ENGINE)
        refused = store.execute(learner, StartPlacement(), Actor.ENGINE)

    assert committed.status is ExecutionStatus.COMMITTED
    assert refused.status is ExecutionStatus.REFUSED
    assert "learner record created learner=logging-learner" in caplog.text
    assert "record action committed learner=logging-learner action=StartPlacement" in (
        caplog.text
    )
    assert "record action refused learner=logging-learner action=StartPlacement" in (
        caplog.text
    )
    assert "Private student name" not in caplog.text
