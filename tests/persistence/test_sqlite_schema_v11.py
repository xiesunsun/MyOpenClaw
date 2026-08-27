from __future__ import annotations

import sqlite3

import pytest

from pickel.persistence.sqlite_schema_v11 import (
    SCHEMA_VERSION,
    UnsupportedSchemaVersionError,
    create_schema,
)


def test_schema_is_v11_and_has_exact_child_package_binding() -> None:
    connection = sqlite3.connect(":memory:")
    create_schema(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    columns = {
        row[1]: (row[3], row[4])
        for row in connection.execute("PRAGMA table_info(agent_delegations)")
    }
    assert columns["child_package_version_id"] == (1, None)
    foreign_keys = {
        row[3]: row[2]
        for row in connection.execute("PRAGMA foreign_key_list(agent_delegations)")
    }
    assert foreign_keys["child_package_version_id"] == "agent_package_versions"


def test_schema_rejects_v10_without_implicit_upgrade() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA user_version = 10")

    with pytest.raises(UnsupportedSchemaVersionError):
        create_schema(connection)
