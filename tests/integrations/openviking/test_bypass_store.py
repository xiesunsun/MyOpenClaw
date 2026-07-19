from pathlib import Path

from myopenclaw.integrations.openviking.bypass_store import OpenVikingBypassStore


def test_bypass_store_round_trip(tmp_path: Path):
    store = OpenVikingBypassStore(tmp_path / "ov.db")
    store.put("s1", {"remote_session_id": "r1", "cursor": 3})
    loaded = store.get("s1")
    assert loaded == {"remote_session_id": "r1", "cursor": 3}
    assert store.get("missing") is None
