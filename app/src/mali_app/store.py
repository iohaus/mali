"""SQLite implementation of the application's durable learner-record port."""

import json
import logging
import sqlite3
from dataclasses import fields
from fractions import Fraction
from hashlib import sha256
from typing import cast
from uuid import uuid4

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
from mali.policy import POLICY_V2, TutorPolicy
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
from mali.views import ClosedMistake, progress_map

from mali_app.clock import Clock, SystemClock, as_storage_time
from mali_app.fresh import FreshSource, SystemFreshSource
from mali_app.schema import DatabasePath, open_database
from mali_app.store_types import (
    AuditResult,
    CurriculumDisplay,
    ExecutionResult,
    ExecutionStatus,
    LearnerTopic,
    LearningClaim,
    QuestionEvidence,
    ReturningLearner,
    TeacherLearnerDetail,
    TeacherLearnerSummary,
    TeachingTrace,
)

_MAX_EXECUTION_ATTEMPTS = 2
_LOG = logging.getLogger(__name__)


class StoreError(Exception):
    """Raised when durable rows cannot form a valid learner record."""


class LearnerNotFound(StoreError):
    """Raised when a requested learner has no durable progress row."""


class LearnerAlreadyRegistered(StoreError):
    """Raised when a registration id already belongs to a learner."""


class CurriculumNotChosen(StoreError):
    """Raised when a learner has not yet adopted any curriculum."""


class TopicNotAdopted(StoreError):
    """Raised when a learner asks to continue a topic they never started."""


class CheckInProgressError(StoreError):
    """Raised when a change must wait for the learner's open check."""


class _RecordConflict(Exception):
    """Abort an in-flight write whose expected row was not current."""


class SQLiteRecordStore:
    """Assemble snapshots and co-commit core plans with their journal entries."""

    def __init__(
        self,
        path: DatabasePath,
        policy: TutorPolicy = POLICY_V2,
        *,
        clock: Clock | None = None,
        fresh: FreshSource | None = None,
    ) -> None:
        self._path = path
        self._policy = policy
        self._clock = SystemClock() if clock is None else clock
        self._fresh = SystemFreshSource() if fresh is None else fresh
        _LOG.debug("SQLiteRecordStore initialized database=%s policy=%s", path, policy)
        self._install_configuration()

    def register(self, learner: LearnerId, display_name: str) -> None:
        """Create a learner who has not yet chosen what to learn."""
        if not display_name.strip():
            raise StoreError("learner display name must not be blank")
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO learner (id, display_name, created_at) VALUES (?, ?, ?)",
                (learner, display_name.strip(), self._now()),
            )
            connection.execute("COMMIT")
            _LOG.info("learner record created learner=%s", learner)
        except sqlite3.IntegrityError as error:
            self._rollback(connection)
            _LOG.info("returning learner recognized learner=%s", learner)
            raise LearnerAlreadyRegistered(
                "learner id is already registered"
            ) from error
        except sqlite3.DatabaseError as error:
            self._rollback(connection)
            _LOG.warning("learner record creation failed learner=%s", learner)
            raise StoreError("could not register learner") from error
        finally:
            connection.close()

    def adopt_curriculum(
        self,
        learner: LearnerId,
        curriculum: Curriculum,
        *,
        title: str,
        summary: str,
        assumed: tuple[str, ...] = (),
    ) -> Snapshot:
        """Save a curriculum and make it the learner's active course of study."""
        if not title.strip():
            raise StoreError("a curriculum needs a learner-facing title")
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            learner_row = connection.execute(
                "SELECT id FROM learner WHERE id = ?", (learner,)
            ).fetchone()
            if learner_row is None:
                raise LearnerNotFound(f"learner {learner!r} was not found")
            open_row = connection.execute(
                "SELECT id FROM checkpoint WHERE learner = ? AND status = 'open'",
                (learner,),
            ).fetchone()
            if open_row is not None:
                raise CheckInProgressError(
                    "finish the current check before changing course"
                )
            now = self._now()
            self._save_curriculum(
                connection, curriculum, title, summary, now, frozenset(assumed)
            )
            connection.execute(
                """
                INSERT INTO progress
                    (learner, curriculum_version, state_bits, placed, target_skill,
                     version, updated_at)
                VALUES (?, ?, 0, 0, NULL, 0, ?)
                ON CONFLICT (learner, curriculum_version) DO NOTHING
                """,
                (learner, curriculum.version, now),
            )
            connection.execute(
                "UPDATE learner SET active_curriculum = ? WHERE id = ?",
                (curriculum.version, learner),
            )
            snapshot = self._load_snapshot(connection, learner, None)
            connection.execute("COMMIT")
            _LOG.info(
                "curriculum adopted learner=%s version=%s skills=%s",
                learner,
                curriculum.version,
                len(curriculum.skills),
            )
            return snapshot
        except (LearnerNotFound, CheckInProgressError):
            self._rollback(connection)
            raise
        except sqlite3.DatabaseError as error:
            self._rollback(connection)
            _LOG.warning("curriculum adoption failed learner=%s", learner)
            raise StoreError("could not adopt the curriculum") from error
        finally:
            connection.close()

    def learner_topics(self, learner: LearnerId) -> ReturningLearner:
        """Read one learner's continuable topics for their home session.

        Time is O(topics for this learner); each row is one adopted
        curriculum, so the result stays small without pagination.
        """
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            learner_row = connection.execute(
                "SELECT id, display_name, active_curriculum FROM learner WHERE id = ?",
                (learner,),
            ).fetchone()
            if learner_row is None:
                raise LearnerNotFound(f"learner {learner!r} was not found")
            topic_rows = connection.execute(
                """
                SELECT p.curriculum_version, p.state_bits, c.title,
                       (SELECT COUNT(*) FROM skill s
                        WHERE s.curriculum_version = p.curriculum_version)
                           AS skill_count
                FROM progress p JOIN curriculum c
                    ON c.version = p.curriculum_version
                WHERE p.learner = ?
                ORDER BY p.updated_at DESC, p.curriculum_version
                """,
                (learner,),
            ).fetchall()
            connection.execute("COMMIT")
        except LearnerNotFound:
            self._rollback(connection)
            raise
        except sqlite3.DatabaseError as error:
            self._rollback(connection)
            raise StoreError("could not read the learner's topics") from error
        finally:
            connection.close()
        active_version = _optional_text(learner_row["active_curriculum"])
        return ReturningLearner(
            learner,
            _text(learner_row["display_name"]),
            tuple(
                LearnerTopic(
                    _text(row["curriculum_version"]),
                    _text(row["title"]),
                    _integer(row["state_bits"]).bit_count(),
                    _integer(row["skill_count"]),
                    active_version == _text(row["curriculum_version"]),
                )
                for row in topic_rows
            ),
        )

    def switch_topic(self, learner: LearnerId, version: str) -> Snapshot:
        """Make one previously adopted topic the learner's active course."""
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            learner_row = connection.execute(
                "SELECT id FROM learner WHERE id = ?", (learner,)
            ).fetchone()
            if learner_row is None:
                raise LearnerNotFound(f"learner {learner!r} was not found")
            open_row = connection.execute(
                "SELECT id FROM checkpoint WHERE learner = ? AND status = 'open'",
                (learner,),
            ).fetchone()
            if open_row is not None:
                raise CheckInProgressError(
                    "finish the current check before changing course"
                )
            progress_row = connection.execute(
                "SELECT curriculum_version FROM progress "
                "WHERE learner = ? AND curriculum_version = ?",
                (learner, version),
            ).fetchone()
            if progress_row is None:
                raise TopicNotAdopted("that topic is not saved for this learner")
            connection.execute(
                "UPDATE learner SET active_curriculum = ? WHERE id = ?",
                (version, learner),
            )
            snapshot = self._load_snapshot(connection, learner, None)
            connection.execute("COMMIT")
            _LOG.info("topic resumed learner=%s version=%s", learner, version)
            return snapshot
        except (LearnerNotFound, CheckInProgressError, TopicNotAdopted):
            self._rollback(connection)
            raise
        except sqlite3.DatabaseError as error:
            self._rollback(connection)
            _LOG.warning("topic switch failed learner=%s", learner)
            raise StoreError("could not switch to the requested topic") from error
        finally:
            connection.close()

    def assumed_skill_codes(self, version: str) -> frozenset[str]:
        """Read which skills of one curriculum are assumed prior knowledge."""
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT code FROM skill WHERE curriculum_version = ? AND assumed = 1",
                (version,),
            ).fetchall()
            return frozenset(_text(row["code"]) for row in rows)
        except sqlite3.DatabaseError as error:
            raise StoreError("could not read the assumed skills") from error
        finally:
            connection.close()

    def curriculum_display(self, version: str) -> CurriculumDisplay:
        """Read the learner-facing title and summary of one saved curriculum."""
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT title, summary FROM curriculum WHERE version = ?",
                (version,),
            ).fetchone()
            if row is None:
                raise StoreError("the requested curriculum is not saved")
            return CurriculumDisplay(_text(row["title"]), _text(row["summary"]))
        except sqlite3.DatabaseError as error:
            raise StoreError("could not read the curriculum description") from error
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
                _LOG.debug(
                    "record action requested learner=%s action=%s actor=%s "
                    "expected_version=%s",
                    learner,
                    type(action).__name__,
                    actor.value,
                    expected_version,
                )
                connection.execute("BEGIN IMMEDIATE")
                checkpoint_identifier = (
                    self._fresh.checkpoint_id()
                    if action.__class__.__name__ in {"StartPlacement", "StartCheck"}
                    else None
                )
                snapshot = self._load_snapshot(
                    connection, learner, checkpoint_identifier
                )
                _LOG.info("loaded snapshot learner=%s", learner)
                _LOG.debug("snapshot=%s", snapshot)
                if (
                    expected_version is not None
                    and snapshot.progress.version != expected_version
                ):
                    self._rollback(connection)
                    _LOG.info(
                        "record action stale learner=%s action=%s expected_version=%s",
                        learner,
                        type(action).__name__,
                        expected_version,
                    )
                    return ExecutionResult(ExecutionStatus.STALE_RECORD, None)
                planned = TutorDesk.plan(action, snapshot, actor)
                if isinstance(planned, Refused):
                    self._rollback(connection)
                    _LOG.info(
                        "record action refused learner=%s action=%s reason=%s",
                        learner,
                        type(action).__name__,
                        planned.reason.value,
                    )
                    return ExecutionResult(
                        ExecutionStatus.REFUSED,
                        snapshot,
                        refusal=planned.reason,
                    )
                now = self._now()
                self._apply_plan(connection, snapshot, planned, now)
                self._append_journal(
                    connection,
                    learner,
                    snapshot.progress.curriculum_version,
                    planned,
                    now,
                )
                committed = self._load_snapshot(connection, learner, None)
                connection.execute("COMMIT")
                _LOG.info(
                    "record action committed learner=%s action=%s version=%s",
                    learner,
                    type(action).__name__,
                    committed.progress.version,
                )
                return ExecutionResult(ExecutionStatus.COMMITTED, committed, planned)
            except _RecordConflict:
                self._rollback(connection)
                _LOG.debug(
                    "record action conflict learner=%s action=%s",
                    learner,
                    type(action).__name__,
                )
            except sqlite3.OperationalError as error:
                self._rollback(connection)
                if "locked" not in str(error).lower():
                    _LOG.exception(
                        "record action database error learner=%s action=%s",
                        learner,
                        type(action).__name__,
                    )
                    raise StoreError("could not update learner record") from error
                _LOG.debug(
                    "record action locked; retrying learner=%s action=%s",
                    learner,
                    type(action).__name__,
                )
            except sqlite3.DatabaseError as error:
                self._rollback(connection)
                _LOG.exception(
                    "record action database error learner=%s action=%s",
                    learner,
                    type(action).__name__,
                )
                raise StoreError("could not update learner record") from error
            finally:
                connection.close()
        _LOG.warning(
            "record action exhausted retries learner=%s action=%s",
            learner,
            type(action).__name__,
        )
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
                WHERE learner = ? AND curriculum_version = ?
                ORDER BY rowid
                """,
                (learner, live.progress.curriculum_version),
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
        except CurriculumNotChosen:
            self._rollback(connection)
            return AuditResult(True, "no curriculum chosen yet")
        except (JournalCorruption, StoreError, sqlite3.DatabaseError) as error:
            self._rollback(connection)
            _LOG.warning("record audit failed learner=%s detail=%s", learner, error)
            return AuditResult(False, f"audit could not read the journal: {error}")
        finally:
            connection.close()
        if _same_progress(replayed, live.progress):
            _LOG.info("record audit passed learner=%s", learner)
            return AuditResult(True, "journal agrees with current progress")
        _LOG.warning("record audit mismatch learner=%s", learner)
        return AuditResult(False, "journal does not agree with current progress")

    def recent_mistakes(
        self, learner: LearnerId, skill: SkillCode, limit: int
    ) -> tuple[ClosedMistake, ...]:
        """Load only closed-check incorrect answers for one skill's teaching context."""
        if type(limit) is not int or limit < 1:
            raise ValueError("recent mistake limit must be a positive integer")
        connection = self._connection()
        try:
            rows = connection.execute(
                """
                SELECT q.params, q.answer_key, a.given
                FROM question AS q
                JOIN checkpoint AS c ON c.id = q.checkpoint_id
                JOIN answer AS a ON a.question_id = q.id
                WHERE c.learner = ?
                  AND c.status <> 'open'
                  AND c.curriculum_version = (
                      SELECT active_curriculum FROM learner WHERE id = ?
                  )
                  AND q.skill = ?
                  AND a.is_correct = 0
                ORDER BY a.answered_at DESC, q.id DESC
                LIMIT ?
                """,
                (learner, learner, skill, limit),
            ).fetchall()
            mistakes = tuple(
                ClosedMistake(
                    skill,
                    _text(_mapping(_decoded(_text(row["params"])))["text"]),
                    _text(row["given"]),
                    _text(row["answer_key"]),
                )
                for row in rows
            )
            _LOG.debug(
                "loaded closed mistakes learner=%s skill=%s count=%s",
                learner,
                skill,
                len(mistakes),
            )
            return tuple(reversed(mistakes))
        except sqlite3.DatabaseError as error:
            raise StoreError("could not load recent mistakes") from error
        finally:
            connection.close()

    def record_teaching_trace(self, trace: TeachingTrace) -> None:
        """Persist one completed teaching turn without changing learner progress."""
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO teaching_trace
                    (id, learner, skill, episode_id, model, prompt_version,
                     policy_version, transcript, tokens_in, tokens_out,
                     episode_outcome, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"trace-{uuid4().hex}",
                    trace.learner,
                    trace.skill,
                    trace.episode_id,
                    trace.model,
                    trace.prompt_version,
                    trace.policy_version,
                    trace.transcript,
                    trace.tokens_in,
                    trace.tokens_out,
                    trace.episode_outcome,
                    self._now(),
                ),
            )
            connection.execute("COMMIT")
            _LOG.debug(
                "teaching trace saved learner=%s episode=%s model=%s "
                "outcome=%s tokens_out=%s",
                trace.learner,
                trace.episode_id,
                trace.model,
                trace.episode_outcome,
                trace.tokens_out,
            )
        except sqlite3.DatabaseError as error:
            self._rollback(connection)
            raise StoreError("could not save teaching trace") from error
        finally:
            connection.close()

    def teacher_dashboard(self) -> tuple[TeacherLearnerSummary, ...]:
        """Read concise learner summaries for the teacher dashboard."""
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT id, display_name FROM learner ORDER BY display_name, id"
            ).fetchall()
            prepared: list[tuple[LearnerId, str, int, int, str | None, int]] = []
            for row in rows:
                learner = learner_id(_text(row["id"]))
                try:
                    snapshot = self._load_snapshot(connection, learner, None)
                except CurriculumNotChosen:
                    prepared.append(
                        (learner, _text(row["display_name"]), 0, 0, None, 0)
                    )
                    continue
                mapped = progress_map(snapshot.progress, snapshot.progress.curriculum)
                evidence_row = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM learning_journal
                    WHERE learner = ? AND curriculum_version = ?
                    """,
                    (learner, snapshot.progress.curriculum_version),
                ).fetchone()
                if evidence_row is None:
                    raise StoreError("could not count learner evidence")
                current_title = _skill_title(
                    snapshot.progress.curriculum, snapshot.progress.target
                )
                prepared.append(
                    (
                        learner,
                        _text(row["display_name"]),
                        len(mapped.mastered),
                        len(mapped.next_up),
                        current_title,
                        _integer(evidence_row["count"]),
                    )
                )
            connection.execute("COMMIT")
        except (StoreError, sqlite3.DatabaseError) as error:
            self._rollback(connection)
            raise StoreError("could not read teacher dashboard") from error
        finally:
            connection.close()
        return tuple(
            TeacherLearnerSummary(
                learner,
                display_name,
                mastered_count,
                ready_count,
                current_title,
                evidence_count,
                self.audit(learner).valid,
            )
            for (
                learner,
                display_name,
                mastered_count,
                ready_count,
                current_title,
                evidence_count,
            ) in prepared
        )

    def teacher_detail(self, learner: LearnerId) -> TeacherLearnerDetail:
        """Read one learner's claims and supporting question evidence."""
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            learner_row = connection.execute(
                "SELECT display_name FROM learner WHERE id = ?", (learner,)
            ).fetchone()
            if learner_row is None:
                raise LearnerNotFound(f"learner {learner!r} was not found")
            try:
                snapshot = self._load_snapshot(connection, learner, None)
            except CurriculumNotChosen:
                connection.execute("COMMIT")
                return TeacherLearnerDetail(
                    learner,
                    _text(learner_row["display_name"]),
                    (),
                    (),
                    (),
                    AuditResult(True, "no curriculum chosen yet"),
                    (),
                )
            mapped = progress_map(snapshot.progress, snapshot.progress.curriculum)
            claims = self._teacher_claims(connection, learner, snapshot)
            connection.execute("COMMIT")
        except LearnerNotFound:
            self._rollback(connection)
            raise
        except (StoreError, sqlite3.DatabaseError) as error:
            self._rollback(connection)
            raise StoreError("could not read teacher evidence") from error
        finally:
            connection.close()
        return TeacherLearnerDetail(
            learner,
            _text(learner_row["display_name"]),
            mapped.mastered,
            mapped.next_up,
            mapped.later,
            self.audit(learner),
            claims,
        )

    def _teacher_claims(
        self, connection: sqlite3.Connection, learner: LearnerId, snapshot: Snapshot
    ) -> tuple[LearningClaim, ...]:
        checkpoint_rows = connection.execute(
            """
            SELECT id, kind, target_skill, status, closed_at
            FROM checkpoint
            WHERE learner = ? AND status <> 'open' AND curriculum_version = ?
            ORDER BY closed_at DESC, id DESC
            """,
            (learner, snapshot.progress.curriculum_version),
        ).fetchall()
        claims = [
            self._checkpoint_claim(connection, learner, snapshot, row)
            for row in checkpoint_rows
        ]
        override_rows = connection.execute(
            """
            SELECT payload, occurred_at
            FROM learning_journal
            WHERE learner = ? AND entry_type = 'OverrideMastery'
              AND curriculum_version = ?
            ORDER BY occurred_at DESC, id DESC
            """,
            (learner, snapshot.progress.curriculum_version),
        ).fetchall()
        claims.extend(
            _override_claim(snapshot, _text(row["payload"]), _text(row["occurred_at"]))
            for row in override_rows
        )
        return tuple(sorted(claims, key=lambda claim: claim.occurred_at, reverse=True))

    def _checkpoint_claim(
        self,
        connection: sqlite3.Connection,
        learner: LearnerId,
        snapshot: Snapshot,
        row: sqlite3.Row,
    ) -> LearningClaim:
        checkpoint = _text(row["id"])
        kind = _text(row["kind"])
        status_value = _text(row["status"])
        occurred_at = _text(row["closed_at"])
        title = _skill_title(
            snapshot.progress.curriculum, _optional_skill(row["target_skill"])
        )
        heading, detail, action_types = _claim_copy(kind, status_value, title)
        questions = tuple(
            QuestionEvidence(
                _text(_mapping(_decoded(_text(question["params"])))["text"]),
                _optional_text(question["given"]),
                None
                if question["is_correct"] is None
                else _integer(question["is_correct"]) == 1,
                _optional_text(question["answered_at"]),
            )
            for question in connection.execute(
                """
                SELECT q.params, a.given, a.is_correct, a.answered_at
                FROM question AS q
                LEFT JOIN answer AS a ON a.question_id = q.id
                WHERE q.checkpoint_id = ?
                ORDER BY q.asked_at, q.id
                """,
                (checkpoint,),
            ).fetchall()
        )
        return LearningClaim(
            heading,
            detail,
            occurred_at,
            _claim_attribution(connection, learner, action_types, occurred_at),
            questions,
        )

    def _install_configuration(self) -> None:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO policy (version, params, created_at)
                VALUES (?, ?, ?)
                """,
                (self._policy.version, _policy_payload(self._policy), self._now()),
            )
            connection.execute("COMMIT")
        except sqlite3.DatabaseError as error:
            self._rollback(connection)
            raise StoreError("could not save tutoring policy configuration") from error
        finally:
            connection.close()

    @staticmethod
    def _save_curriculum(
        connection: sqlite3.Connection,
        curriculum: Curriculum,
        title: str,
        summary: str,
        now: str,
        assumed: frozenset[str] = frozenset(),
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO curriculum (version, title, summary, loaded_at)
            VALUES (?, ?, ?, ?)
            """,
            (curriculum.version, title.strip(), summary.strip(), now),
        )
        for skill in curriculum.skills:
            connection.execute(
                """
                INSERT OR IGNORE INTO skill
                    (curriculum_version, code, bit_index, title, card, template,
                     assumed)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    curriculum.version,
                    skill.code,
                    skill.bit_index,
                    skill.title,
                    skill.card,
                    _template_payload(skill.template),
                    1 if skill.code in assumed else 0,
                ),
            )
        for skill_reference, requirements in curriculum.requirements:
            for required in requirements:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO skill_requires
                        (curriculum_version, skill, requires)
                    VALUES (?, ?, ?)
                    """,
                    (curriculum.version, skill_reference, required),
                )

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
        learner_row = connection.execute(
            "SELECT active_curriculum FROM learner WHERE id = ?", (learner,)
        ).fetchone()
        if learner_row is None:
            raise LearnerNotFound(f"learner {learner!r} was not found")
        active_version = learner_row["active_curriculum"]
        if active_version is None:
            raise CurriculumNotChosen(
                f"learner {learner!r} has not chosen what to learn"
            )
        row = connection.execute(
            """
            SELECT learner, curriculum_version, state_bits, placed, target_skill,
                   version
            FROM progress WHERE learner = ? AND curriculum_version = ?
            """,
            (learner, _text(active_version)),
        ).fetchone()
        if row is None:
            raise StoreError("the active curriculum has no learner progress row")
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
                     closed_at, curriculum_version)
                VALUES (?, ?, ?, ?, 'open', NULL, ?, NULL, ?)
                """,
                (
                    checkpoint.identifier,
                    snapshot.progress.learner,
                    _checkpoint_kind_value(checkpoint.kind),
                    checkpoint.target,
                    now,
                    snapshot.progress.curriculum_version,
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
        curriculum_version: str,
        plan: ActionPlan,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO learning_journal
                (id, learner, entry_type, payload, prior_version, occurred_at,
                 curriculum_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._fresh.journal_id(),
                learner,
                type(plan.entry.action).__name__,
                _plan_payload(plan),
                plan.entry.prior_version,
                now,
                curriculum_version,
            ),
        )

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("ROLLBACK")


def _skill_title(curriculum: Curriculum, code: SkillCode | None) -> str | None:
    if code is None:
        return None
    try:
        return next(skill.title for skill in curriculum.skills if skill.code == code)
    except StopIteration as error:
        raise StoreError("saved record references an unknown skill") from error


def _claim_copy(
    kind: str, status: str, title: str | None
) -> tuple[str, str, tuple[str, ...]]:
    if kind == "placement" and status == "certified":
        return (
            "Starting map",
            "A short check set this learner's starting point.",
            ("CertifyPlacement",),
        )
    if title is None:
        raise StoreError("a completed skill check needs a skill title")
    if status == "passed":
        return (
            f"Mastered {title}",
            "The completed check supports this achievement.",
            ("PassCheck",),
        )
    return (
        f"Practice continues for {title}",
        "This check shows where more practice will help.",
        ("FailCheck", "CloseStale"),
    )


def _claim_attribution(
    connection: sqlite3.Connection,
    learner: LearnerId,
    action_types: tuple[str, ...],
    occurred_at: str,
) -> str:
    marks = ", ".join("?" for _ in action_types)
    row = connection.execute(
        f"""
        SELECT payload FROM learning_journal
        WHERE learner = ? AND entry_type IN ({marks}) AND occurred_at <= ?
        ORDER BY occurred_at DESC, id DESC
        LIMIT 1
        """,
        (learner, *action_types, occurred_at),
    ).fetchone()
    if row is None:
        return "Mali"
    payload = _mapping(_decoded(_text(row["payload"])))
    actor = _text(payload["actor"])
    return {
        "admin": "Administrator",
        "engine": "Mali",
        "instructor": "Tutor",
        "student": "Student",
        "teacher": "Teacher",
    }.get(actor, "Mali")


def _override_claim(
    snapshot: Snapshot, payload: str, occurred_at: str
) -> LearningClaim:
    data = _mapping(_decoded(payload))
    action = _mapping(data["action"])
    fields = _mapping(action["fields"])
    title = _skill_title(
        snapshot.progress.curriculum, skill_code(_text(fields["skill"]))
    )
    if title is None:
        raise StoreError("teacher note must name a skill")
    return LearningClaim(
        f"Mastered {title}",
        f"Teacher note: {_text(fields['note'])}",
        occurred_at,
        "Teacher",
        (),
    )


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


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
                "recent_mistake_limit": policy.flow_budget.recent_mistake_limit,
            },
            "instructor_prompt_version": policy.instructor_prompt_version,
            "item_writer_prompt_version": policy.item_writer_prompt_version,
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
            _integer(
                flow_budget.get(
                    "recent_mistake_limit",
                    POLICY_V2.flow_budget.recent_mistake_limit,
                )
            ),
        ),
        _text(
            data.get("instructor_prompt_version", POLICY_V2.instructor_prompt_version)
        ),
        _text(
            data.get("item_writer_prompt_version", POLICY_V2.item_writer_prompt_version)
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
