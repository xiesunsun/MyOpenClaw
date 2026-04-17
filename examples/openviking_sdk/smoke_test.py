from __future__ import annotations

import os
import sys
import time
from typing import Any

import openviking as ov


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def print_section(title: str) -> None:
    print(f"\n== {title} ==")


def dump_result(label: str, value: Any) -> None:
    print(f"{label}: {value}")


def main() -> int:
    base_url = require_env("OPENVIKING_BASE_URL")
    api_key = require_env("OPENVIKING_USER_KEY")
    agent_id = require_env("OPENVIKING_AGENT_ID")
    test_root = os.environ.get(
        "OPENVIKING_SDK_TEST_ROOT",
        "viking://resources/sdk-smoke",
    )
    session_id = f"sdk-smoke-{int(time.time())}"

    print_section("Connect")
    client = ov.SyncHTTPClient(
        url=base_url,
        api_key=api_key,
        agent_id=agent_id,
        timeout=120.0,
    )
    print(f"client: {type(client).__name__}")

    try:
        client.initialize()
        dump_result("initialize", "ok")

        print_section("Status")
        dump_result("status", client.get_status())

        print_section("Filesystem")
        try:
            client.mkdir(test_root)
            dump_result("mkdir", f"created {test_root}")
        except Exception as exc:
            dump_result("mkdir", f"non-fatal: {type(exc).__name__}: {exc}")

        dump_result("stat", client.stat(test_root))
        dump_result("ls(viking://resources)", client.ls("viking://resources", simple=True))

        print_section("Find")
        find_result = client.find(
            query="OpenViking coding agent integration",
            target_uri="viking://resources/openviking-readme",
            limit=3,
        )
        dump_result(
            "find.resources",
            [(item.uri, round(item.score, 4)) for item in find_result.resources],
        )
        dump_result(
            "find.skills",
            [(item.uri, round(item.score, 4)) for item in find_result.skills],
        )

        print_section("Search Without Session")
        search_result = client.search(
            query="OpenViking coding agent integration",
            target_uri="viking://resources/openviking-readme",
            limit=3,
        )
        dump_result(
            "search.resources",
            [(item.uri, round(item.score, 4)) for item in search_result.resources],
        )

        print_section("Session")
        created = client.create_session()
        dump_result("create_session", created)
        created_session_id = created.get("session_id") or session_id

        client.add_message(
            session_id=created_session_id,
            role="user",
            content="Please help my coding agent use OpenViking effectively.",
        )
        client.add_message(
            session_id=created_session_id,
            role="assistant",
            content="I can use find, session management, and skills for OpenViking integration.",
        )
        dump_result("add_message", "2 messages added")

        print_section("Search With Session")
        try:
            session_search = client.search(
                query="how should my coding agent use OpenViking",
                session_id=created_session_id,
                limit=3,
            )
            dump_result(
                "session_search.resources",
                [(item.uri, round(item.score, 4)) for item in session_search.resources],
            )
            dump_result(
                "session_search.skills",
                [(item.uri, round(item.score, 4)) for item in session_search.skills],
            )
        except Exception as exc:
            dump_result(
                "session_search_error",
                f"{type(exc).__name__}: {exc}",
            )

        print_section("Commit")
        dump_result("commit_session", client.commit_session(created_session_id))
        dump_result("get_session", client.get_session(created_session_id))
    finally:
        client.close()
        dump_result("close", "ok")

    return 0


if __name__ == "__main__":
    sys.exit(main())
