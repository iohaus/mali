"""Command-line entry points for the local Mali application."""

import argparse
from collections.abc import Sequence

from mali.ids import learner_id

from mali_app.demo import demo_curriculum, seed_demo
from mali_app.store import LearnerNotFound, SQLiteRecordStore, StoreError

_DEFAULT_DATABASE = "mali.db"


def run(arguments: Sequence[str] | None = None) -> int:
    """Run one CLI command and return a process exit status."""
    parser = _parser()
    parsed = parser.parse_args(arguments)
    if parsed.command == "demo-seed":
        store = SQLiteRecordStore(parsed.database, demo_curriculum())
        snapshot = seed_demo(store)
        print(
            f"seeded {snapshot.progress.learner}: "
            f"{snapshot.progress.mask.bit_count()} skill complete"
        )
        return 0
    if parsed.command == "audit":
        store = SQLiteRecordStore(parsed.database, demo_curriculum())
        try:
            result = store.audit(learner_id(parsed.learner))
        except (LearnerNotFound, StoreError) as error:
            print(f"audit failed: {error}")
            return 1
        print(result.detail)
        return 0 if result.valid else 1
    parser.error("a command is required")
    return 2


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
    return parser
