import sqlite3

from mali_app.schema import apply_migrations, open_database


def test_open_database_enables_wal_and_creates_the_record_schema() -> None:
    connection = open_database(":memory:")
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()
        assert {
            "learner",
            "curriculum",
            "skill",
            "skill_requires",
            "policy",
            "progress",
            "checkpoint",
            "question",
            "answer",
            "learning_journal",
            "teaching_trace",
        } <= tables
        assert version == (3,)
    finally:
        connection.close()


def test_migrations_are_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        apply_migrations(connection)
        apply_migrations(connection)
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    finally:
        connection.close()


def test_migration_renames_the_checkpoint_archive_column() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE checkpoint (placement_data TEXT)")
        connection.execute(
            """
            CREATE TABLE teaching_trace (
                id TEXT PRIMARY KEY,
                learner TEXT NOT NULL,
                skill TEXT NOT NULL,
                model TEXT NOT NULL,
                transcript TEXT NOT NULL,
                tokens_in INTEGER NOT NULL,
                tokens_out INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute("PRAGMA user_version = 1")

        apply_migrations(connection)

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(checkpoint)")
        }
        assert "estimate" in columns
        assert "placement_data" not in columns
    finally:
        connection.close()
