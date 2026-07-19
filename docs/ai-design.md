# Mali — How GPT-5.6 Is Used

Mali uses GPT-5.6 in exactly three places. Each is bounded, stateless, and
carries zero authority over the certified learner record. This document
describes what each flow does, what it is given, and what it cannot affect.

---

## Design principle

The one-sentence security posture: **containment, not detection.**

Mali does not try to recognize prompt injection or social engineering. Instead,
it makes their success structurally worthless: no path from GPT-5.6's output
leads to certified progress, answer keys, or verdicts. The model proposes;
the domain core decides.

---

## Flow 1 — Curriculum Author

**When it runs:** a new learner names a topic (e.g. "long division",
"introductory Python", "music theory basics").

**What it does:** produces a structured curriculum — a set of skills in
prerequisite order, a short teaching card per skill, and a parameterized
question template per skill. Every template must be machine-checkable
(integer or fraction arithmetic answer rules).

**What bounds it:**
- The draft is validated by the domain core before adoption. Any violation
  — an impossible prerequisite order, a missing template, a question that
  cannot be auto-graded — returns a typed rejection, and GPT-5.6 gets exactly
  one repair attempt before the flow fails honestly.
- A draft that fails validation is never adopted. The learner sees a clear
  error, not a broken curriculum.
- Switching topics never overwrites a learner's existing evidence chain.

**What it cannot do:** adopt a curriculum the core rejects; touch any learner's
certified progress; modify anyone else's record.

---

## Flow 2 — Instructor

**When it runs:** the student opens or continues a lesson for the current
target skill.

**What it does:** streams conversational teaching turns — explanations,
examples, responses to student questions — and may propose a target skill
change or request a practice check through typed function calls.

**What it is given:**
- The current target skill's teaching card
- A plain-English summary of what the student has mastered and what is next
- The student's last few wrong answers on this skill (question text and
  answer only — no keys)
- The student's chat turns

**What it is never given:** answer keys of any open or future check;
probability estimates; other learners' records; the student's display name
(prompts address the student as "you").

**What bounds it:**
- Function calls that propose a skill change or start a check are routed
  through the domain core's rule engine. A refused proposal (skill not yet
  ready, check already open, etc.) returns to the model as a typed refusal
  reason, not an error — the Instructor's job is to turn the refusal into
  helpful prose ("we'll get there; first let's nail X").
- A budget of turns and function calls per session; exhaustion is a typed
  outcome (the lesson closes politely) and does not affect the record.
- If the API is unavailable, the server falls back to static teaching cards
  from the curriculum. Checks, grading, and the teacher panel remain fully
  functional with no model connection at all.

**What it cannot do:** write certified progress; change answer keys; affect
other learners; skip a check or mark a skill mastered.

---

## Flow 3 — Item Writer

**When it runs:** a practice check needs its next question displayed.

**What it does:** takes a computed question instance (already sampled and
keyed by the domain core) and rephrases the question text into more engaging
prose.

**What it is given:** the question parameters and phrasing constraints only.
**It is never given the answer key.**

**What bounds it:**
- A validator checks that every load-bearing value appears verbatim in the
  rephrasing, the question has exactly the right structure, and no
  meta-commentary ("As an AI…") is present.
- On validation failure, it retries once with the failure reason, then falls
  back to a deterministic plain rendering. The check always runs; a bad
  rephrasing is replaced, not propagated.
- Grading compares the student's answer against the computed key only — the
  prose the model produced is never part of the grading path.

**What it cannot do:** change the question, alter the answer key, or
influence whether the student passes.

---

## Error handling

Every failure is typed and has exactly one handling rule:

| Failure | Handling |
|---|---|
| API timeout or outage | Degrade to static teaching cards and plain question rendering; record unaffected |
| Structured output fails schema | Counts as one retry attempt |
| Item Writer prose fails validation | Retry with reason, then deterministic fallback |
| Model proposes a disallowed action | Typed refusal returned to the model as data |
| Moderation block on a teaching turn | Replace with static card excerpt; flag for review |

At no degradation level does anything touch the safety of the certified
learner record.
