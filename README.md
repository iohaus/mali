# Mali

**The model teaches. The structure certifies.**

Mali is a one-on-one adaptive tutor. Tell it what you want to learn — anything — and it designs
a step-by-step curriculum for that topic, teaches each skill conversationally, and certifies
mastery only through auto-graded practice checks. A teacher or parent can expand every mastery
claim into the exact questions asked and answers given, and a single command re-derives the whole
record from its evidence journal.

> 🎥 **Demo video:** _link added at submission time_

## What Mali does

- **Ask for anything.** A learner names a topic; GPT-5.6 drafts a full curriculum — small skills,
  the order they build on each other, and a practice pattern per skill with a *computed* answer
  key. Mali's deterministic core validates the draft and rejects anything that breaks its rules.
- **Start in the right place.** A short adaptive check finds what the learner already knows and
  produces a progress map: Mastered, Next up, Later.
- **Learn one solid step at a time.** Streamed conversational lessons focus on exactly one ready
  skill. Ask to skip ahead and the tutor turns the refusal into a plan — the model can propose,
  but the rules decide.
- **Prove it.** Quick checks are machine-graded against computed keys; passing advances certified
  progress by exactly one skill. The model never writes progress.
- **Show your work.** The teacher view expands every claim into its recorded questions, answers,
  and timestamps, and `mali audit` replays the journal to confirm it reproduces the live record.

## Quickstart

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
cd app
uv sync
export OPENAI_API_KEY=sk-...
uv run mali serve
```

Open http://127.0.0.1:8000, enter a name, and tell Mali what you'd like to learn.

All commands run from the `app/` directory; `uv sync` there also installs the local tutoring
core package automatically.

## Try it in two minutes (sample data)

```bash
cd app
uv run mali demo-seed        # creates demo-learner with a completed skill (safe to re-run)
uv run mali serve
```

- Visit http://127.0.0.1:8000/teacher → open **Mali demo learner** → expand the mastery claim to
  see the questions, answers, and timestamps behind it.
- Verify the record from the command line:

  ```bash
  uv run mali audit --learner demo-learner
  # journal agrees with current progress   (exit code 0)
  ```

- Then go to the home page and start a fresh journey with any topic of your own.

## Model configuration

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | credential for the default OpenAI provider | — |
| `MALI_MODEL_PROVIDER` | `openai` or `qwen` | `openai` |
| `MALI_MODEL` | model override | `gpt-5.6` (openai), `qwen3.6-flash` (qwen) |
| `MALI_MODEL_BASE_URL` | endpoint override (absolute http/https URL) | provider default |
| `MALI_MODEL_API_KEY` | provider-neutral credential override | — |
| `DASHSCOPE_API_KEY` | credential when `MALI_MODEL_PROVIDER=qwen` | — |

Variables must be **exported in the shell** — the app reads the process environment and does not
load `.env` files.

Without a configured model, the server still runs: lessons fall back to static teaching cards and
questions render in plain form. Curriculum drafting needs a model connection, so the seeded demo
learner is the way to explore keyless.

## How trust works

- **Answer keys are computed, never authored.** Every question comes from a verified template;
  grading compares against the computed key, never against model output.
- **The model proposes; the rules decide.** Study targets and checks requested by the model are
  re-checked by the deterministic core, and refusals come back as typed results.
- **Every claim carries its evidence.** Progress changes commit atomically with a journal entry
  recording the questions, answers, and verdicts that justified them.
- **The record is replayable.** `mali audit` folds the journal from zero and must reproduce the
  live record exactly — divergence fails loudly.

## Built with Codex and GPT-5.6

**GPT-5.6 at runtime** powers three bounded flows, none of which can write certified progress:

1. **Curriculum drafting** — one structured-output call turns a learner's topic into skills,
   prerequisites, and practice patterns; the core validates the draft and returns a typed
   rejection for exactly one repair attempt before failing honestly.
2. **Conversational lessons** — streamed teaching turns with typed function calls; off-limits
   proposals bounce back as typed refusals the tutor renders as coaching.
3. **Question rendering** — practice questions are rephrased engagingly, checked by a validator,
   and fall back to deterministic plain text if the rendering is rejected.

**Codex at build time** drove a spec-first workflow: the tutoring core is a dependency-free
Python package whose invariant and property-based test suite served as machine-checkable ground
truth — Codex iterated against the suite until every law passed, and CI keeps those laws
enforced on every push. Key decisions shaped this way: the evidence journal as the system of
record (kill the process mid-lesson and nothing about the learner is lost), computed-only answer
keys, refusals as typed values rather than exceptions, and a one-repair-then-fail contract for
model drafts.

## Running the tests

```bash
cd mali && uv sync --all-groups && uv run pytest   # core: invariants, property suite, law checks
cd app  && uv sync && uv run pytest                # app: adapters, flows, end-to-end surfaces
```

The core suite is property-based (Hypothesis) and the whole repository type-checks strictly.

## Troubleshooting

- **No API key** — the server runs in a reduced mode (static lessons, plain questions); use
  `demo-seed` to explore, or export a key to enable curriculum drafting and live lessons.
- **Port already in use** — `uv run mali serve --port 8010`.
- **Start fresh** — stop the server, delete `app/mali.db`, re-run `uv run mali demo-seed`.
- **Running from the repository root** — `uv run --project app mali serve` works but writes
  `mali.db` into your current directory; prefer `cd app`.

## License

MIT — see [LICENSE](LICENSE).
