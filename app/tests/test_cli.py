from pathlib import Path

import pytest
from fastapi import FastAPI

from mali_app import cli
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


def test_serve_starts_the_local_app_with_the_requested_network_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: dict[str, object] = {}
    logging_options: list[bool] = []

    def fake_run(app: FastAPI, *, host: str, port: int) -> None:
        started["app"] = app
        started["host"] = host
        started["port"] = port

    def fake_configure_logging(*, verbose: bool) -> None:
        logging_options.append(verbose)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setattr(cli, "configure_console_logging", fake_configure_logging)

    result = run(
        (
            "serve",
            "--database",
            str(tmp_path / "serve.db"),
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
            "--verbose",
        )
    )

    assert result == 0
    assert isinstance(started["app"], FastAPI)
    assert started["host"] == "0.0.0.0"
    assert started["port"] == 9001
    assert logging_options == [True]
