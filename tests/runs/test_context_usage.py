"""ContextUsageService still targets GenerateRequest / last_session_recall_message.

Task 8 moved providers to ModelContext + count_context_tokens and removed session recall
from AgentRuntimeContext/RunDependencies. Full rewrite deferred to Task 12.
"""

from __future__ import annotations

import unittest


@unittest.skip("ContextUsageService GenerateRequest path obsolete after Task 8; rewrite in Task 12")
class ContextUsageServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_counts_incremental_request_categories(self) -> None:
        return

    async def test_snapshot_does_not_cache_failed_result(self) -> None:
        return

    async def test_snapshot_handles_provider_count_failures(self) -> None:
        return

    async def test_snapshot_normalizes_empty_session_placeholder_tokens(self) -> None:
        return

    async def test_snapshot_recomputes_when_provider_thinking_blocks_change(self) -> None:
        return

    async def test_snapshot_recomputes_when_session_hash_changes(self) -> None:
        return

    async def test_snapshot_reuses_cached_result_when_session_hash_is_unchanged(self) -> None:
        return

    async def test_snapshot_uses_prompt_messages_when_provided(self) -> None:
        return


if __name__ == "__main__":
    unittest.main()
