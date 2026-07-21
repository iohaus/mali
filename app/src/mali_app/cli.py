"""Command-line entry points for the local Mali application."""

import argparse
import logging
from collections.abc import Sequence

from dotenv import load_dotenv
import uvicorn
from mali.ids import learner_id

from mali_app.api import create_app
from mali_app.demo import seed_demo
from mali_app.logging_setup import configure_console_logging
from mali_app.model_gateway import GatewayConfigurationError, ModelGateway
from mali_app.model_providers import create_model_gateway_from_environment
from mali_app.store import LearnerNotFound, SQLiteRecordStore, StoreError

_DEFAULT_DATABASE = "mali.db"
_LOG = logging.getLogger(__name__)


def run(arguments: Sequence[str] | None = None) -> int:
    """Run one CLI command and return a process exit status."""
    load_dotenv(override=False)
    parser = _parser()
    parsed = parser.parse_args(arguments)
    if parsed.command == "demo-seed":
        store = SQLiteRecordStore(parsed.database)
        snapshot = seed_demo(store)
        print(
            f"seeded {snapshot.progress.learner}: "
            f"{snapshot.progress.mask.bit_count()} skill complete"
        )
        return 0
    if parsed.command == "audit":
        store = SQLiteRecordStore(parsed.database)
        try:
            result = store.audit(learner_id(parsed.learner))
        except (LearnerNotFound, StoreError) as error:
            print(f"audit failed: {error}")
            return 1
        print(result.detail)
        return 0 if result.valid else 1
    if parsed.command == "serve":
        configure_console_logging(verbose=parsed.verbose)
        gateway = _gateway_from_environment()
        use_models = gateway is not None
        _LOG.info(
            "starting local server host=%s port=%s database=%s model_flows=%s",
            parsed.host,
            parsed.port,
            parsed.database,
            use_models,
        )
        app = create_app(
            parsed.database,
            enable_instructor=use_models,
            enable_item_writer=use_models,
            model_gateway=gateway,
        )
        uvicorn.run(app, host=parsed.host, port=parsed.port)
        _LOG.info("local server stopped")
        return 0
    parser.error("a command is required")
    return 2


def _gateway_from_environment() -> ModelGateway | None:
    """Use live model flows only when the selected provider is configured."""
    try:
        return create_model_gateway_from_environment()
    except GatewayConfigurationError as error:
        _LOG.info("model flows disabled reason=%s", error)
        return None


def main() -> None:
    """Run the console script."""
    raise SystemExit(run())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mali")
    commands = parser.add_subparsers(dest="command")
    audit = commands.add_parser(
        "audit", help="compare a learner journal to live progress"
    )
    audit.add_argument("--database", default=_DEFAULT_DATABASE)
    audit.add_argument("--learner", required=True)
    seed = commands.add_parser("demo-seed", help="create a deterministic local demo")
    seed.add_argument("--database", default=_DEFAULT_DATABASE)
    serve = commands.add_parser(
        "serve", help="start the local student and teacher site"
    )
    serve.add_argument("--database", default=_DEFAULT_DATABASE)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--verbose", action="store_true", help="include detailed application logs"
    )
    return parser
