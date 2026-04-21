from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import openviking as ov


REQUIRED_ENV_VARS = (
    "OPENVIKING_BASE_URL",
    "OPENVIKING_USER_KEY",
    "OPENVIKING_ACCOUNT_ID",
    "OPENVIKING_USER_ID",
    "OPENVIKING_AGENT_ID",
)


@dataclass(frozen=True)
class OpenVikingIdentity:
    base_url: str
    account_id: str
    user_id: str
    user_key: str
    agent_id: str

    @property
    def memory_root(self) -> str:
        return f"viking://user/{self.user_id}/memories"


def main() -> int:
    args = parse_args()
    identity = load_identity_from_env()
    client = build_client(identity)
    client.initialize()
    try:
        print_header("OpenViking Identity")
        print(f"account_id: {identity.account_id}")
        print(f"user_id: {identity.user_id}")
        print(f"agent_id: {identity.agent_id}")
        print(f"memory_root: {identity.memory_root}")

        print_profile(client, identity)
        for category in args.categories:
            print_memory_category(client, identity, category=category)

        if args.query:
            print_memory_search(
                client,
                identity,
                query=args.query,
                limit=args.limit,
                score_threshold=args.score_threshold,
            )
    finally:
        client.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect OpenViking user long-term memory for the configured user and agent."
    )
    parser.add_argument(
        "--query",
        default="偏好,风格,喜欢",
        help="Query used for top-k memory retrieval. Pass an empty string to skip search.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Maximum number of memories to return from find().",
    )
    parser.add_argument(
        "--score-threshold",
        type=float,
        default=None,
        help="Optional minimum retrieval score for find().",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=["preferences", "entities", "events"],
        choices=["preferences", "entities", "events"],
        help="Memory categories to list and read.",
    )
    return parser.parse_args()


def load_identity_from_env() -> OpenVikingIdentity:
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    return OpenVikingIdentity(
        base_url=os.environ["OPENVIKING_BASE_URL"],
        account_id=os.environ["OPENVIKING_ACCOUNT_ID"],
        user_id=os.environ["OPENVIKING_USER_ID"],
        user_key=os.environ["OPENVIKING_USER_KEY"],
        agent_id=os.environ["OPENVIKING_AGENT_ID"],
    )


def build_client(identity: OpenVikingIdentity) -> Any:
    return ov.SyncHTTPClient(
        url=identity.base_url,
        api_key=identity.user_key,
        agent_id=identity.agent_id,
        account=identity.account_id,
        user=identity.user_id,
        timeout=30.0,
    )


def print_profile(client: Any, identity: OpenVikingIdentity) -> None:
    print_header("Profile")
    profile_uri = f"{identity.memory_root}/profile.md"
    print(f"uri: {profile_uri}")
    print(read_or_error(client, profile_uri))


def print_memory_category(
    client: Any,
    identity: OpenVikingIdentity,
    *,
    category: str,
) -> None:
    print_header(category.capitalize())
    category_uri = f"{identity.memory_root}/{category}"
    try:
        entries = client.ls(category_uri, simple=True, node_limit=100)
    except Exception as exc:
        print(f"list failed: {type(exc).__name__}: {exc}")
        return

    memory_files = visible_markdown_files(entries)
    print(f"uri: {category_uri}")
    print(f"count: {len(memory_files)}")
    for index, uri in enumerate(memory_files, start=1):
        print(f"\n--- {category} {index}: {uri} ---")
        print(read_or_error(client, uri))


def print_memory_search(
    client: Any,
    identity: OpenVikingIdentity,
    *,
    query: str,
    limit: int,
    score_threshold: float | None,
) -> None:
    print_header("Top-K Memory Retrieval")
    result = client.find(
        query=query,
        target_uri=identity.memory_root,
        limit=limit,
        score_threshold=score_threshold,
    )
    print(f"query: {query}")
    print(f"limit: {limit}")
    if score_threshold is not None:
        print(f"score_threshold: {score_threshold}")
    print(f"memory_matches: {len(result.memories)}")

    for index, memory in enumerate(result.memories, start=1):
        print(f"\n--- match {index} ---")
        print(f"uri: {memory.uri}")
        print(f"score: {memory.score:.4f}")
        if memory.category:
            print(f"category: {memory.category}")
        if memory.match_reason:
            print(f"reason: {memory.match_reason}")
        if memory.abstract:
            print("abstract:")
            print(memory.abstract)
        elif memory.overview:
            print("overview:")
            print(memory.overview)


def visible_markdown_files(entries: Iterable[Any]) -> list[str]:
    files: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        name = entry.rstrip("/").rsplit("/", 1)[-1]
        if not name.endswith(".md"):
            continue
        if name.startswith("."):
            continue
        files.append(entry)
    return files


def read_or_error(client: Any, uri: str) -> str:
    try:
        return client.read(uri, limit=-1) or ""
    except Exception as exc:
        return f"read failed: {type(exc).__name__}: {exc}"


def print_header(title: str) -> None:
    print(f"\n=== {title} ===")


if __name__ == "__main__":
    sys.exit(main())
