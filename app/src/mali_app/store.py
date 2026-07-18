"""SQLite implementation of the application's durable learner-record port."""

import json
import sqlite3
from dataclasses import fields
from fractions import Fraction
from hashlib import sha256
from typing import cast

from mali.actions import (
    Action,
    Actor,
    CertifyPlacement,
    CloseStale,
    FailCheck,
    PassCheck,
)
from mali.checkpoint import Answer, CheckPoint, CheckPointKind, Question
from mali.curriculum import Curriculum, Skill
from mali.desk import TutorDesk
from mali.errors import JournalCorruption
from mali.ids import (
    CheckPointId,
    LearnerId,
    SkillCode,
    checkpoint_id,
    learner_id,
    question_id,
    skill_code,
)
from mali.journal import Journal
from mali.plans import ActionPlan, CheckPointWrite, JournalEntry, ProgressWrite
from mali.policy import POLICY_V1, TutorPolicy
from mali.progress import Progress
from mali.rules import Refused
from mali.snapshot import Snapshot
from mali.templates import (
    AnswerType,
    Constraint,
    ConstraintKind,
    DisplayValue,
    ParameterDomain,
    QuestionInstance,
    QuestionTemplate,
)

from mali_app.clock import Clock, SystemClock, as_storage_time
from mali_app.fresh import FreshSource, SystemFreshSource
from mali_app.schema import DatabasePath, open_database
from mali_app.store_types import AuditResult, ExecutionResult, ExecutionStatus

_MAX_EXECUTION_ATTEMPTS = 2
_CURRICULUM_TITLE = "Mali curriculum"


class StoreError(Exception):
    """Raised when durable rows cannot form a valid learner record."""


class LearnerNotFound(StoreError):
    """Raised when a requested learner has no durable progress row."""


class _RecordConflict(Exception):
    """Abort an in-flight write whose expected row was not current."""


class SQLiteRecordStore:
    """Assemble snapshots and co-commit core plans with their journal entries."""

    def __init__(
        self,
        path: DatabasePath,
        curriculum: Curriculum,
        policy: TutorPolicy = POLICY_V1,
        *,
        clock: Clock | None = None,
        fresh: FreshSource | None = None,
    ) -> None:
        self._path = path
        self._curriculum = curriculum
        self._policy = policy
        self._clock = SystemClock() if clock is None else clock
        self._fresh = SystemFreshSource() if fresh is None else fresh
        self._install_configuration()

    def register(self, learner: LearnerId, display_name: str) -> Snapshot:
        """Create a learner and their empty, not-yet-placed progress record."""
        if not display_name.strip():
            raise StoreError("learner display name must not be blank")
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            connection.execute(
                "INSERT INTO learner (id, display_name, created_at) VALUES (?, ?, ?)",
                (learner, display_name.strip(), now),
            )
            connection.execute(
                """
                INSERT INTO progress
                    (learner, curriculum_version, state_bits, placed, target_skill,
                     version, updated_at)
                VALUES (?, ?, 0, 0, NULL, 0, ?)
                """,
                (learner, self._curriculum.version, now),
            )
            snapshot = self._load_snapshot(connection, learner, None)
            connection.execute("COMMIT")
            return snapshot
        except sqlite3.DatabaseError as error:
            self._rollback(connection)
            raise StoreError("could not register learner") from error
        finally:
            connection.close()

    def snapshot(self, learner: LearnerId) -> Snapshot:
        """Read one learner's current record in a consistent read transaction."""
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            snapshot = self._load_snapshot(connection, learner, None)
            connection.execute("COMMIT")
            return snapshot
        except sqlite3.DatabaseError as error:
            self._rollback(connection)
            raise StoreError("could not read learner record") from error
        finally:
            connection.close()

    def execute(
        self,
        learner: LearnerId,
        action: Action,
        actor: Actor,
        *,
        expected_version: int | None = None,
    ) -> ExecutionResult:
        """Re-plan and atomically persist one requested tutoring action."""
        for _ in range(_MAX_EXECUTION_ATTEMPTS):
            connection = self._connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                checkpoint_identifier = (
                    self._fresh.checkpoint_id()
                    if action.__class__.__name__ in {"StartPlacement", "StartCheck"}
                    else None
                )
                snapshot = self._load_snapshot(
                    connection, learner, checkpoint_identifier
                )
                if (
                    expected_version is not None
                    and snapshot.progress.version != expected_version
                ):
                    self._rollback(connection)
                    return ExecutionResult(ExecutionStatus.STALE_RECORD, None)
                planned = TutorDesk.plan(action, snapshot, actor)
                if isinstance(planned, Refused):
                    self._rollback(connection)
                    return ExecutionResult(
                        ExecutionStatus.REFUSED,
                        snapshot,
                        refusal=planned.reason,
                    )
                now = self._now()
                self._apply_plan(connection, snapshot, planned, now)
                self._append_journal(connection, learner, planned, now)
                committed = self._load_snapshot(connection, learner, None)
                connection.execute("COMMIT")
                return ExecutionResult(ExecutionStatus.COMMITTED, committed, planned)
            except _RecordConflict:
                self._rollback(connection)
            except sqlite3.OperationalError as error:
                self._rollback(connection)
                if "locked" not in str(error).lower():
                    raise StoreError("could not update learner record") from error
            except sqlite3.DatabaseError as error:
                self._rollback(connection)
                raise StoreError("could not update learner record") from error
            finally:
                connection.close()
        return ExecutionResult(ExecutionStatus.STALE_RECORD, None)

    def audit(self, learner: LearnerId) -> AuditResult:
        """Fold a learner's saved journal and compare it to live progress."""
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            live = self._load_snapshot(connection, learner, None)
            rows = connection.execute(
                """
                SELECT payload FROM learning_journal
                WHERE learner = ?
                ORDER BY rowid
                """,
                (learner,),
            ).fetchall()
            plans = tuple(
                _plan_from_payload(
                    _text(row["payload"]),
                    live.progress.learner,
                    live.progress.curriculum,
                )
                for row in rows
            )
            initial = Progress(
                live.progress.learner,
                live.progress.curriculum_version,
                0,
                False,
                None,
                0,
                live.progress.curriculum,
            )
            replayed = Journal.replay(initial, plans)
            connection.execute("COMMIT")
        except (JournalCorruption, StoreError, sqlite3.DatabaseError) as error:
            self._rollback(connection)
            return AuditResult(False, f"audit could not read the journal: {error}")
        finally:
            connection.close()
        if _same_progress(replayed, live.progress):
            return AuditResult(True, "journal agrees with current progress")
        return AuditResult(False, "journal does not agree with current progress")

    def _install_configuration(self) -> None:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            connection.execute(
                """
                INSERT OR IGNORE INTO curriculum (version, title, loaded_at)
                VALUES (?, ?, ?)
                """,
                (self._curriculum.version, _CURRICULUM_TITLE, now),
            )
            for skill in self._curriculum.skills:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO skill
                        (curriculum_version, code, bit_index, title, card, template)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._curriculum.version,
                        skill.code,
                        skill.bit_index,
                        skill.title,
                        skill.card,
                        _template_payload(skill.template),
                    ),
                )
            for skill, requirements in self._curriculum.requirements:
                for required in requirements:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO skill_requires
                            (curriculum_version, skill, requires)
                        VALUES (?, ?, ?)
                        """,
                        (self._curriculum.version, skill, required),
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO policy (version, params, created_at)
                VALUES (?, ?, ?)
                """,
                (self._policy.version, _policy_payload(self._policy), now),
            )
            connection.execute("COMMIT")
        except sqlite3.DatabaseError as error:
            self._rollback(connection)
            raise StoreError("could not save curriculum configuration") from error
        finally:
            connection.close()

    def _connection(self) -> sqlite3.Connection:
        connection = open_database(self._path)
        connection.row_factory = sqlite3.Row
        return connection

    def _now(self) -> str:
        return as_storage_time(self._clock.now())

    def _load_snapshot(
        self,
        connection: sqlite3.Connection,
        learner: LearnerId,
        fresh_checkpoint_id: CheckPointId | None,
    ) -> Snapshot:
        row = connection.execute(
            """
            SELECT learner, curriculum_version, state_bits, placed, target_skill,
                   version
            FROM progress WHERE learner = ?
            """,
            (learner,),
        ).fetchone()
        if row is None:
            raise LearnerNotFound(f"learner {learner!r} was not found")
        curriculum = self._load_curriculum(connection, _text(row["curriculum_version"]))
        progress = Progress(
            learner_id(_text(row["learner"])),
            curriculum.version,
            _integer(row["state_bits"]),
            _integer(row["placed"]) == 1,
            _optional_skill(row["target_skill"]),
            _integer(row["version"]),
            curriculum,
        )
        checkpoint = self._load_open_checkpoint(connection, progress.learner)
        policy = self._load_policy(connection)
        return Snapshot(progress, checkpoint, policy, fresh_checkpoint_id)

    def _load_curriculum(
        self, connection: sqlite3.Connection, version: str
    ) -> Curriculum:
        rows = connection.execute(
            """
            SELECT code, bit_index, title, card, template FROM skill
            WHERE curriculum_version = ? ORDER BY bit_index
            """,
            (version,),
        ).fetchall()
        if not rows:
            raise StoreError("current curriculum has no saved skills")
        skills = tuple(
            Skill(
                skill_code(_text(row["code"])),
                _integer(row["bit_index"]),
                _text(row["title"]),
                _text(row["card"]),
                _template_from_payload(_text(row["template"])),
            )
            for row in rows
        )
        requirements_by_skill: dict[str, list[str]] = {
            str(skill.code): [] for skill in skills
        }
        requirement_rows = connection.execute(
            """
            SELECT skill, requires FROM skill_requires
            WHERE curriculum_version = ? ORDER BY skill, requires
            """,
            (version,),
        ).fetchall()
        for row in requirement_rows:
            requirements_by_skill[_text(row["skill"])].append(_text(row["requires"]))
        curriculum = Curriculum.load(
            skills,
            tuple(
                (skill, tuple(requirements))
                for skill, requirements in requirements_by_skill.items()
                if requirements
            ),
        )
        if curriculum.version != version:
            raise StoreError("saved curriculum does not match its version")
        return curriculum

    def _load_policy(self, connection: sqlite3.Connection) -> TutorPolicy:
        row = connection.execute(
            "SELECT params FROM policy WHERE version = ?", (self._policy.version,)
        ).fetchone()
        if row is None:
            raise StoreError("current policy is not saved")
        policy = _policy_from_payload(_text(row["params"]))
        if policy.version != self._policy.version:
            raise StoreError("saved policy does not match its version")
        return policy

    def _load_open_checkpoint(
        self, connection: sqlite3.Connection, learner: LearnerId
    ) -> CheckPoint | None:
        row = connection.execute(
            """
            SELECT id, kind, target_skill FROM checkpoint
            WHERE learner = ? AND status = 'open'
            """,
            (learner,),
        ).fetchone()
        if row is None:
            return None
        identifier = checkpoint_id(_text(row["id"]))
        kind_value = _text(row["kind"])
        if kind_value == "placement":
            kind = CheckPointKind.PLACEMENT
        elif kind_value == "mastery_check":
            kind = CheckPointKind.CHECK
        else:
            raise StoreError("saved checkpoint has an unknown kind")
        question_rows = connection.execute(
            """
            SELECT q.id, q.skill, q.params, q.answer_key, a.given, a.is_correct
            FROM question AS q
            LEFT JOIN answer AS a ON a.question_id = q.id
            WHERE q.checkpoint_id = ? ORDER BY q.asked_at, q.id
            """,
            (identifier,),
        ).fetchall()
        questions = tuple(
            _question_from_row(question_row) for question_row in question_rows
        )
        return CheckPoint(
            identifier,
            kind,
            _optional_skill(row["target_skill"]),
            questions,
        )

    def _apply_plan(
        self,
        connection: sqlite3.Connection,
        snapshot: Snapshot,
        plan: ActionPlan,
        now: str,
    ) -> None:
        for write in plan.writes:
            if isinstance(write, ProgressWrite):
                self._write_progress(connection, snapshot, plan, write, now)
            else:
                self._write_checkpoint(connection, snapshot, plan, write, now)

    def _write_progress(
        self,
        connection: sqlite3.Connection,
        snapshot: Snapshot,
        plan: ActionPlan,
        write: ProgressWrite,
        now: str,
    ) -> None:
        progress = write.progress
        cursor = connection.execute(
            """
            UPDATE progress
            SET state_bits = ?, placed = ?, target_skill = ?, version = ?,
                updated_at = ?
            WHERE learner = ? AND curriculum_version = ? AND version = ?
            """,
            (
                progress.mask,
                int(progress.placed),
                progress.target,
                progress.version,
                now,
                snapshot.progress.learner,
                snapshot.progress.curriculum_version,
                plan.entry.prior_version,
            ),
        )
        if cursor.rowcount != 1:
            raise _RecordConflict

    def _write_checkpoint(
        self,
        connection: sqlite3.Connection,
        snapshot: Snapshot,
        plan: ActionPlan,
        write: CheckPointWrite,
        now: str,
    ) -> None:
        checkpoint = write.checkpoint
        if checkpoint is None:
            if snapshot.checkpoint is None:
                raise _RecordConflict
            cursor = connection.execute(
                """
                UPDATE checkpoint SET status = ?, closed_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (
                    _closed_status(plan.entry.action),
                    now,
                    snapshot.checkpoint.identifier,
                ),
            )
            if cursor.rowcount != 1:
                raise _RecordConflict
            return
        if snapshot.checkpoint is None:
            connection.execute(
                """
                INSERT INTO checkpoint
                    (id, learner, kind, target_skill, status, estimate, opened_at,
                     closed_at)
                VALUES (?, ?, ?, ?, 'open', NULL, ?, NULL)
                """,
                (
                    checkpoint.identifier,
                    snapshot.progress.learner,
                    _checkpoint_kind_value(checkpoint.kind),
                    checkpoint.target,
                    now,
                ),
            )
        elif checkpoint.identifier != snapshot.checkpoint.identifier:
            raise _RecordConflict
        self._write_questions(connection, checkpoint, now)

    def _write_questions(
        self, connection: sqlite3.Connection, checkpoint: CheckPoint, now: str
    ) -> None:
        existing = {
            _text(row["id"])
            for row in connection.execute(
                "SELECT id FROM question WHERE checkpoint_id = ?",
                (checkpoint.identifier,),
            )
        }
        for question in checkpoint.questions:
            if question.identifier not in existing:
                connection.execute(
                    """
                    INSERT INTO question
                        (id, checkpoint_id, skill, params, answer_key, rendered_hash,
                         asked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        question.identifier,
                        checkpoint.identifier,
                        question.skill,
                        _question_payload(question.instance),
                        question.instance.key,
                        sha256(question.instance.text.encode()).hexdigest(),
                        now,
                    ),
                )
            if question.answer is not None:
                answer_row = connection.execute(
                    "SELECT given, is_correct FROM answer WHERE question_id = ?",
                    (question.identifier,),
                ).fetchone()
                if answer_row is None:
                    connection.execute(
                        """
                        INSERT INTO answer (question_id, given, is_correct, answered_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            question.identifier,
                            question.answer.value,
                            int(question.answer.correct),
                            now,
                        ),
                    )
                elif _text(answer_row["given"]) != question.answer.value or _integer(
                    answer_row["is_correct"]
                ) != int(question.answer.correct):
                    raise _RecordConflict

    def _append_journal(
        self,
        connection: sqlite3.Connection,
        learner: LearnerId,
        plan: ActionPlan,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO learning_journal
                (id, learner, entry_type, payload, prior_version, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._fresh.journal_id(),
                learner,
                type(plan.entry.action).__name__,
                _plan_payload(plan),
                plan.entry.prior_version,
                now,
            ),
        )

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("ROLLBACK")


def _template_payload(template: QuestionTemplate | None) -> str:
    if template is None:
        return "null"
    return _encoded(
        {
            "parameters": [
                {
                    "name": parameter.name,
                    "values": [str(value) for value in parameter.values],
                }
                for parameter in template.parameters
            ],
            "key_expression": template.key_expression,
            "plain_template": template.plain_template,
            "answer_type": template.answer_type.value,
            "constraints": [
                {"kind": constraint.kind.value, "names": list(constraint.names)}
                for constraint in template.constraints
            ],
            "display_values": [
                {"name": value.name, "expression": value.expression}
                for value in template.display_values
            ],
            "options": list(template.options),
        }
    )


def _template_from_payload(payload: str) -> QuestionTemplate | None:
    value = _decoded(payload)
    if value is None:
        return None
    data = _mapping(value)
    parameters = tuple(
        ParameterDomain(
            _text(parameter["name"]),
            tuple(Fraction(_text(item)) for item in _sequence(parameter["values"])),
        )
        for parameter in (_mapping(item) for item in _sequence(data["parameters"]))
    )
    constraints = tuple(
        Constraint(
            ConstraintKind(_text(constraint["kind"])),
            tuple(_text(name) for name in _sequence(constraint["names"])),
        )
        for constraint in (_mapping(item) for item in _sequence(data["constraints"]))
    )
    display_values = tuple(
        DisplayValue(_text(item["name"]), _text(item["expression"]))
        for item in (_mapping(value) for value in _sequence(data["display_values"]))
    )
    return QuestionTemplate(
        parameters,
        _text(data["key_expression"]),
        _text(data["plain_template"]),
        AnswerType(_text(data["answer_type"])),
        constraints,
        display_values,
        tuple(_text(option) for option in _sequence(data["options"])),
    )


def _policy_payload(policy: TutorPolicy) -> str:
    return _encoded(
        {
            "version": policy.version,
            "certify_threshold": str(policy.certify_threshold),
            "question_budget": policy.question_budget,
            "pass_rule": {
                "needed": policy.pass_rule.needed,
                "asked": policy.pass_rule.asked,
            },
            "miss_rates": [[kind.value, str(rate)] for kind, rate in policy.miss_rates],
            "lucky_rates": [
                [kind.value, str(rate)] for kind, rate in policy.lucky_rates
            ],
            "stale_after_seconds": int(policy.stale_after.total_seconds()),
            "flow_budget": {
                "max_turns": policy.flow_budget.max_turns,
                "max_requests": policy.flow_budget.max_requests,
                "max_output_tokens": policy.flow_budget.max_output_tokens,
                "max_episode_tokens": policy.flow_budget.max_episode_tokens,
                "item_writer_retries": policy.flow_budget.item_writer_retries,
            },
        }
    )


def _policy_from_payload(payload: str) -> TutorPolicy:
    from datetime import timedelta

    from mali.policy import FlowBudget, PassRule

    data = _mapping(_decoded(payload))
    pass_rule = _mapping(data["pass_rule"])
    flow_budget = _mapping(data["flow_budget"])
    return TutorPolicy(
        _text(data["version"]),
        Fraction(_text(data["certify_threshold"])),
        _integer(data["question_budget"]),
        PassRule(_integer(pass_rule["needed"]), _integer(pass_rule["asked"])),
        tuple(
            (AnswerType(_text(kind)), Fraction(_text(rate)))
            for kind, rate in (_pair(item) for item in _sequence(data["miss_rates"]))
        ),
        tuple(
            (AnswerType(_text(kind)), Fraction(_text(rate)))
            for kind, rate in (_pair(item) for item in _sequence(data["lucky_rates"]))
        ),
        timedelta(seconds=_integer(data["stale_after_seconds"])),
        FlowBudget(
            _integer(flow_budget["max_turns"]),
            _integer(flow_budget["max_requests"]),
            _integer(flow_budget["max_output_tokens"]),
            _integer(flow_budget["max_episode_tokens"]),
            _integer(flow_budget["item_writer_retries"]),
        ),
    )


def _question_payload(instance: QuestionInstance) -> str:
    return _encoded(
        {
            "values": [[name, str(value)] for name, value in instance.values],
            "text": instance.text,
            "answer_type": instance.answer_type.value,
            "options": list(instance.options),
            "plain_text_contains_key": instance.plain_text_contains_key,
        }
    )


def _question_from_row(row: sqlite3.Row) -> Question:
    data = _mapping(_decoded(_text(row["params"])))
    instance = QuestionInstance(
        tuple(
            (_text(name), Fraction(_text(value)))
            for name, value in (_pair(item) for item in _sequence(data["values"]))
        ),
        _text(row["answer_key"]),
        _text(data["text"]),
        AnswerType(_text(data["answer_type"])),
        tuple(_text(option) for option in _sequence(data["options"])),
        _boolean(data["plain_text_contains_key"]),
    )
    raw_answer = row["given"]
    answer = (
        None
        if raw_answer is None
        else Answer(_text(raw_answer), _integer(row["is_correct"]) == 1)
    )
    return Question(
        question_id(_text(row["id"])), skill_code(_text(row["skill"])), instance, answer
    )


def _plan_payload(plan: ActionPlan) -> str:
    writes: list[dict[str, object]] = []
    for write in plan.writes:
        if isinstance(write, ProgressWrite):
            writes.append(
                {
                    "type": "progress",
                    "curriculum_version": write.progress.curriculum_version,
                    "mask": write.progress.mask,
                    "placed": write.progress.placed,
                    "target": write.progress.target,
                    "version": write.progress.version,
                }
            )
        else:
            writes.append(
                {
                    "type": "checkpoint",
                    "checkpoint": _checkpoint_evidence(write.checkpoint),
                }
            )
    return _encoded(
        {
            "action": {
                "type": type(plan.entry.action).__name__,
                "fields": {
                    field.name: _event_value(getattr(plan.entry.action, field.name))
                    for field in fields(plan.entry.action)
                },
            },
            "actor": plan.entry.actor.value,
            "prior_version": plan.entry.prior_version,
            "writes": writes,
        }
    )


def _checkpoint_evidence(checkpoint: CheckPoint | None) -> object:
    if checkpoint is None:
        return None
    return {
        "id": checkpoint.identifier,
        "kind": checkpoint.kind.value,
        "target": checkpoint.target,
        "questions": [
            {
                "id": question.identifier,
                "skill": question.skill,
                "key": question.instance.key,
                "text": question.instance.text,
                "answer": None
                if question.answer is None
                else {
                    "value": question.answer.value,
                    "correct": question.answer.correct,
                },
            }
            for question in checkpoint.questions
        ],
    }


def _event_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return repr(value)


def _plan_from_payload(
    payload: str, learner: LearnerId, curriculum: Curriculum
) -> ActionPlan:
    data = _mapping(_decoded(payload))
    writes = tuple(
        ProgressWrite(
            Progress(
                learner,
                _text(write["curriculum_version"]),
                _integer(write["mask"]),
                _boolean(write["placed"]),
                _optional_skill(write["target"]),
                _integer(write["version"]),
                curriculum,
            )
        )
        for write in (_mapping(item) for item in _sequence(data["writes"]))
        if _text(write["type"]) == "progress"
    )
    return ActionPlan(
        writes,
        JournalEntry(
            CertifyPlacement(),
            Actor(_text(data["actor"])),
            _integer(data["prior_version"]),
        ),
    )


def _closed_status(action: Action) -> str:
    if isinstance(action, PassCheck):
        return "passed"
    if isinstance(action, (FailCheck, CloseStale)):
        return "failed"
    if isinstance(action, CertifyPlacement):
        return "certified"
    raise StoreError("a checkpoint close action has no saved status")


def _checkpoint_kind_value(kind: CheckPointKind) -> str:
    return "placement" if kind is CheckPointKind.PLACEMENT else "mastery_check"


def _same_progress(left: Progress, right: Progress) -> bool:
    return (
        left.curriculum_version == right.curriculum_version
        and left.mask == right.mask
        and left.placed == right.placed
        and left.target == right.target
        and left.version == right.version
    )


def _encoded(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _decoded(payload: str) -> object:
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise StoreError("saved record payload is not readable") from error


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StoreError("saved record payload must be an object")
    mapped: dict[str, object] = {}
    raw_mapping = cast(dict[object, object], value)
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise StoreError("saved record object keys must be text")
        mapped[key] = item
    return mapped


def _sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise StoreError("saved record payload must be a list")
    return list(cast(list[object], value))


def _pair(value: object) -> tuple[object, object]:
    values = _sequence(value)
    if len(values) != 2:
        raise StoreError("saved record pair must contain two values")
    return values[0], values[1]


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StoreError("saved record value must be text")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise StoreError("saved record value must be an integer")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise StoreError("saved record value must be true or false")
    return value


def _optional_skill(value: object) -> SkillCode | None:
    return None if value is None else skill_code(_text(value))
