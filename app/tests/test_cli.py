from pathlib import Path

import pytest

from mali_app.cli import run


def test_demo_seed_and_audit_commands_share_a_durable_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = str(tmp_path / "demo.db")

    assert run(("demo-seed", "--database", database)) == 0
    assert run(("audit", "--database", database, "--learner", "demo-learner")) == 0

    captured = capsys.readouterr()
    assert "seeded demo-learner" in captured.out
    assert "journal agrees" in captured.out
