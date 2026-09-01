import sqlite3
from pathlib import Path

from pickel.persistence.sqlite_schema_v13 import create_schema as create_v13_schema
from pickel.persistence.sqlite_schema_v14 import SCHEMA_VERSION, create_schema
from pickel.persistence.sqlite_v13_migration import migrate_v13_to_v14


def test_new_database_is_v14_and_has_active_plan_column() -> None:
    connection = sqlite3.connect(":memory:")
    create_schema(connection)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(agent_run_states)").fetchall()
    }
    assert "active_plan_json" in columns


def test_v13_migration_preserves_rows_and_adds_null_plan(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    connection = sqlite3.connect(db_path)
    create_v13_schema(connection)
    connection.commit()
    connection.close()

    result = migrate_v13_to_v14(db_path)
    assert result.backup_path.is_file()
    with sqlite3.connect(db_path) as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 14
        assert (
            migrated.execute("SELECT active_plan_json FROM agent_run_states").fetchall()
            == []
        )
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 13
