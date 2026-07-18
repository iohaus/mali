# Mali

Mali is a one-on-one adaptive tutor. It helps learners start at the right
place, focus on a useful next step, and build confidence over time.

## Development

The tutoring core is a dependency-free Python package. It is intentionally
kept separate from future web and AI integrations so its learning decisions
remain predictable and easy to test.

```bash
cd mali
uv sync --all-groups
uv run pytest
```

Run the full local quality gate with:

```bash
cd mali
uv run ruff format --check
uv run ruff check
uv run pyright
uv run pytest
```

## Current status

The initial foundation validates curricula and deterministic question templates,
then carries learner progress and assessment checkpoints as immutable typed
records. The next increment adds placement estimates and question selection.
