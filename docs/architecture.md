# Mali — System Architecture

Mali is a one-on-one adaptive tutor. A student names a topic, Mali builds a
curriculum for it, teaches one skill at a time through conversation, and
certifies mastery only through machine-graded practice checks. A teacher or
parent can view an auditable record of every claim and the exact evidence
behind it.

---

## How it is structured

Mali is organized into three clear layers, each with a distinct job:

```
┌──────────────────────────────────────────────────────────┐
│  PRODUCT SURFACE  (FastAPI + SSE)                        │
│   student chat · progress map · teacher panel            │
├──────────────────────────────────────────────────────────┤
│  DOMAIN CORE  (pure Python, zero dependencies)           │
│   curriculum structure · learner progress · check rules  │
│   evidence journal · deterministic grading               │
├─────────────────────────┬────────────────────────────────┤
│  AI FLOWS (GPT-5.6)     │  RECORD (SQLite)               │
│  teach · render questions│  progress · journal · policy  │
│  zero authority over     │  curriculum · skill stats     │
│  certified progress      │                               │
└─────────────────────────┴────────────────────────────────┘
```

### The domain core

The `mali` Python package is the heart of the system. It is:

- **Pure** — no network calls, no database access, no clocks, no randomness
  inside the package. Time, random seeds, and database ids are supplied by
  the server as data, never fetched.
- **Deterministic** — given the same inputs the core always produces the same
  result, on every machine. This is what makes the journal audit meaningful.
- **Self-contained** — zero runtime dependencies beyond the Python standard
  library. The test suite runs without any infrastructure.

The core defines the rules that govern what can happen: what the student is
ready to learn next, when a check can start, what counts as a passing answer,
and when progress is certified. Nothing outside the core can bypass these
rules.

### The record

Every certified fact about a learner is stored in SQLite with a corresponding
journal entry written in the same database transaction. The record is the
source of truth — not the AI, not the session, not the conversation. Kill the
process mid-lesson and the next session picks up exactly where the student
was, because all context is rebuilt from the record at the start of each
interaction.

### The AI flows

GPT-5.6 participates in three bounded interactions:

| Flow | What it does | What it cannot do |
|---|---|---|
| Curriculum Author | Drafts skills, prerequisites, and question templates for a named topic | Adopt a draft the core rejects |
| Instructor | Teaches the current target skill conversationally | Write certified progress |
| Item Writer | Rephrases a computed question instance into engaging prose | Change the question or its answer key |

The AI never writes the learner record. Every proposed action passes through
the domain core's rule engine before any state changes.

---

## Data flow for a single lesson

```
student opens lesson
  → server reads learner record (one read transaction)
  → GPT-5.6 Instructor streams teaching turns (SSE)
  → student requests a check
  → core checks the request against current state (allowed / refused)
  → core generates a question (deterministic, with computed answer key)
  → GPT-5.6 Item Writer rephrases the question for engagement
  → student answers
  → core grades against computed key (model output never touches grading)
  → if passing: progress committed atomically with a journal entry
  → teacher panel reflects the update immediately
```

---

## What makes this different from a plain LLM tutor

A typical LLM tutor teaches whatever the conversation drifts toward, has no
record of what the student has actually mastered, and its praise is
indistinguishable from a hallucination. Mali separates the two concerns:

- **The AI teaches.** GPT-5.6 brings conversational warmth, clear
  explanations, and engaging question prose.
- **The rules certify.** The domain core decides what is ready to be learned,
  grades all answers against computed keys, and makes every progress claim a
  verifiable fact over recorded evidence — not an opinion from the model.
