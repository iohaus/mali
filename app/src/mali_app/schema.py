"""Versioned SQLite schema for Mali's durable learner record."""

import sqlite3
from pathlib import Path

DatabasePath = str | Path

_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE learner (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE curriculum (
            version TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            loaded_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE skill (
            curriculum_version TEXT NOT NULL REFERENCES curriculum(version),
            code TEXT NOT NULL,
            bit_index INTEGER NOT NULL,
            title TEXT NOT NULL,
            card TEXT NOT NULL,
            template TEXT NOT NULL,
            PRIMARY KEY (curriculum_version, code)
        )
        """,
        """
        CREATE TABLE skill_requires (
            curriculum_version TEXT NOT NULL REFERENCES curriculum(version),
            skill TEXT NOT NULL,
            requires TEXT NOT NULL,
            PRIMARY KEY (curriculum_version, skill, requires)
        )
        """,
        """
        CREATE TABLE policy (
            version TEXT PRIMARY KEY,
            params TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE progress (
            learner TEXT NOT NULL REFERENCES learner(id),
            curriculum_version TEXT NOT NULL REFERENCES curriculum(version),
            state_bits INTEGER NOT NULL,
            placed INTEGER NOT NULL CHECK (placed IN (0, 1)),
            target_skill TEXT,
            version INTEGER NOT NULL CHECK (version >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (learner, curriculum_version)
        )
        """,
        """
        CREATE TABLE checkpoint (
            id TEXT PRIMARY KEY,
            learner TEXT NOT NULL REFERENCES learner(id),
            kind TEXT NOT NULL CHECK (kind IN ('placement', 'mastery_check')),
            target_skill TEXT,
            status TEXT NOT NULL
                CHECK (status IN ('open', 'passed', 'failed', 'certified')),
            placement_data TEXT,
            opened_at TEXT NOT NULL,
            closed_at TEXT
        )
        """,
        """
        CREATE UNIQUE INDEX open_checkpoint_per_learner
        ON checkpoint(learner) WHERE status = 'open'
        """,
        """
        CREATE TABLE question (
            id TEXT PRIMARY KEY,
            checkpoint_id TEXT NOT NULL REFERENCES checkpoint(id),
            skill TEXT NOT NULL,
            params TEXT NOT NULL,
            answer_key TEXT NOT NULL,
            rendered_hash TEXT NOT NULL,
            asked_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE answer (
            question_id TEXT PRIMARY KEY REFERENCES question(id),
            given TEXT NOT NULL,
            is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
            answered_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE learning_journal (
            id TEXT PRIMARY KEY,
            learner TEXT NOT NULL REFERENCES learner(id),
            entry_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            prior_version INTEGER NOT NULL CHECK (prior_version >= 0),
            occurred_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX journal_by_learner_version
        ON learning_journal(learner, prior_version, occurred_at, id)
        """,
        """
        CREATE TABLE teaching_trace (
            id TEXT PRIMARY KEY,
            learner TEXT NOT NULL REFERENCES learner(id),
            skill TEXT NOT NULL,
            model TEXT NOT NULL,
            transcript TEXT NOT NULL,
            tokens_in INTEGER NOT NULL CHECK (tokens_in >= 0),
            tokens_out INTEGER NOT NULL CHECK (tokens_out >= 0),
            created_at TEXT NOT NULL
        )
        """,
    ),
    ("ALTER TABLE checkpoint RENAME COLUMN placement_data TO estimate",),
    (
        """
        ALTER TABLE teaching_trace
        ADD COLUMN episode_id TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE teaching_trace
        ADD COLUMN prompt_version TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE teaching_trace
        ADD COLUMN policy_version TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE teaching_trace
        ADD COLUMN episode_outcome TEXT NOT NULL DEFAULT 'completed'
        """,
    ),
    (
        """
        CREATE TABLE learning_path (
            learner TEXT PRIMARY KEY REFERENCES learner(id),
            topic TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            steps TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ),
    (
        "DROP TABLE learning_path",
        """
        ALTER TABLE learner
        ADD COLUMN active_curriculum TEXT REFERENCES curriculum(version)
        """,
        """
        UPDATE learner SET active_curriculum = (
            SELECT curriculum_version FROM progress
            WHERE progress.learner = learner.id LIMIT 1
        )
        """,
        "ALTER TABLE curriculum ADD COLUMN summary TEXT NOT NULL DEFAULT ''",
        """
        ALTER TABLE learning_journal
        ADD COLUMN curriculum_version TEXT NOT NULL DEFAULT ''
        """,
        """
        UPDATE learning_journal SET curriculum_version = COALESCE(
            (
                SELECT curriculum_version FROM progress
                WHERE progress.learner = learning_journal.learner LIMIT 1
            ),
            ''
        )
        """,
        """
        ALTER TABLE checkpoint
        ADD COLUMN curriculum_version TEXT NOT NULL DEFAULT ''
        """,
        """
        UPDATE checkpoint SET curriculum_version = COALESCE(
            (
                SELECT curriculum_version FROM progress
                WHERE progress.learner = checkpoint.learner LIMIT 1
            ),
            ''
        )
        """,
    ),
)


def open_database(path: DatabasePath) -> sqlite3.Connection:
    """Open a SQLite record store configured for transactional durability."""
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    apply_migrations(connection)
    return connection


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply each unapplied schema migration in one SQLite transaction."""
    current = connection.execute("PRAGMA user_version").fetchone()
    if current is None:
        raise sqlite3.DatabaseError("SQLite did not return a schema version")
    version = current[0]
    if type(version) is not int or version < 0:
        raise sqlite3.DatabaseError("SQLite returned an invalid schema version")
    if version > len(_MIGRATIONS):
        raise sqlite3.DatabaseError("database schema is newer than this application")
    for target, statements in enumerate(_MIGRATIONS[version:], start=version + 1):
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {target}")
            connection.execute("COMMIT")
        except sqlite3.DatabaseError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
