# Mali — Trust and Verification

Mali makes strong claims about what teachers see when they open a student's
progress map. This document explains how those claims are enforced, how they
can be independently verified, and what the audit command does.

---

## The core claims

**1. Mastery is evidence, not opinion.**
Every skill marked as mastered corresponds to a recorded practice check: the
exact questions asked, the answers given, the timestamp, and the verdict.
No model output can write this record.

**2. Answer keys are computed, never authored.**
Questions are generated from parameterized templates. The answer key is
computed by the domain core from the question parameters. GPT-5.6 rephrases
questions for engagement but never sees the key and cannot alter it. Grading
compares the student's answer directly against the computed key.

**3. The model proposes; the rules decide.**
When GPT-5.6 requests a skill change or a practice check on behalf of the
student, the domain core re-checks the request against the current learner
state. A disallowed request returns a typed refusal — not an error, not a
silent skip — that the Instructor renders into helpful feedback for the
student.

**4. Every progress change is atomic with its evidence.**
When a check passes and progress advances, the state update and the journal
entry recording the evidence are written in the same database transaction.
Claim and evidence can never be out of sync.

**5. The record survives anything.**
All context for an interaction is read from the database at its start. A
process crash, a network drop, or an API outage mid-lesson loses at most the
sentence in flight — never a certified fact about the learner.

---

## What the teacher panel shows

The teacher view is a direct query over the journal. For each mastered skill:

- The questions that were asked (as they were displayed to the student)
- The student's answers and whether each was correct
- The timestamps
- The overall verdict (passed / failed / overridden)

Nothing in the teacher panel is reconstructed or inferred. If it is shown,
it happened, and it is in the journal.

---

## The audit command

```bash
uv run mali audit --learner <learner-id>
```

This command independently re-derives the learner's current certified progress
from the raw journal entries, replaying every recorded transition from scratch
and recomputing answer keys from the witnessed question parameters. If the
derived state matches the live record exactly, the command exits 0. Any
divergence — a missing entry, a tampered verdict, a reordered event — fails
loudly with the offending entry identified.

This means the teacher panel is not only readable; it is independently
reproducible. The record is its own audit trail.

---

## What the test suite checks

The `mali` core library has a property-based test suite (using Hypothesis)
that:

- Generates random curricula and random sequences of valid and invalid actions
- Verifies that the core's safety invariants hold after every single step
- Verifies that the journal replay always produces the same final state as
  the live record
- Verifies that a tampered journal (reordered entries, altered answer,
  forged verdict) raises an error, loudly

These tests run on every push to CI. The invariants are not documentation —
they are machine-checked on every change.

---

## Degraded operation

If the OpenAI API is unavailable, Mali degrades automatically:

| Level | Condition | Teaching | Checks & grading |
|---|---|---|---|
| Normal | API available | GPT-5.6 streamed lessons | GPT-5.6 question phrasing |
| Partial | Item Writer failures | Unchanged | Deterministic plain questions |
| Offline | Gateway down | Static teaching cards | Fully functional (no model needed) |

Placement, practice checks, pass/fail grading, and the teacher panel are
model-free by construction. A judge can verify this by running
`uv run mali demo-seed && uv run mali serve` without an API key and navigating
to the teacher panel — the evidence is all there.
