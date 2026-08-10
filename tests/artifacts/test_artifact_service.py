from __future__ import annotations

from collections.abc import Callable
import hashlib
from pathlib import Path

import pytest

from pickel.artifacts.artifact import ArtifactReference
from pickel.artifacts.artifact_service import (
    ArtifactIntegrityError,
    ArtifactService,
)
from pickel.artifacts.filesystem_blob_store import FilesystemBlobStore
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore


@pytest.fixture(params=["memory", "sqlite"])
def artifact_store(request, tmp_path: Path):
    factories: dict[str, Callable[[], object]] = {
        "memory": InMemoryRuntimeStore,
        "sqlite": lambda: SQLiteRuntimeStore(tmp_path / "runtime.db"),
    }
    return factories[request.param]()


def test_create_artifact_keeps_bytes_out_of_sqlite_metadata(artifact_store) -> None:
    blobs = InMemoryBlobStore()
    service = ArtifactService(
        artifact_store=artifact_store,
        blob_store=blobs,
    )

    reference = service.create_artifact(
        data=b"image-bytes",
        media_type="image/png",
        display_name="chart.png",
    )

    assert reference.artifact_id.startswith("artifact_")
    assert reference.display_name == "chart.png"
    assert reference.size_bytes == len(b"image-bytes")
    assert service.load_artifact_bytes(reference) == b"image-bytes"
    metadata = artifact_store.load_artifact(reference.artifact_id)
    assert metadata is not None
    assert metadata.blob_key.startswith("sha256/")


def test_create_same_content_is_idempotent(artifact_store) -> None:
    service = ArtifactService(
        artifact_store=artifact_store,
        blob_store=InMemoryBlobStore(),
    )

    first = service.create_artifact(
        data=b"same",
        media_type="audio/mpeg",
        display_name="first.mp3",
    )
    second = service.create_artifact(
        data=b"same",
        media_type="audio/mpeg",
        display_name="second.mp3",
    )

    assert first.artifact_id == second.artifact_id
    assert first.digest == second.digest
    assert second.display_name == "second.mp3"


def test_load_rejects_tampered_reference(artifact_store) -> None:
    service = ArtifactService(
        artifact_store=artifact_store,
        blob_store=InMemoryBlobStore(),
    )
    reference = service.create_artifact(
        data=b"payload",
        media_type="application/pdf",
    )
    tampered = ArtifactReference(
        artifact_id=reference.artifact_id,
        digest="0" * 64,
        media_type=reference.media_type,
        size_bytes=reference.size_bytes,
    )

    with pytest.raises(ArtifactIntegrityError, match="不匹配"):
        service.load_artifact_bytes(tampered)


def test_filesystem_blob_store_uses_content_addressed_path(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path / "blobs")
    digest = hashlib.sha256(b"payload").hexdigest()

    key = store.put_blob(digest=digest, data=b"payload")

    assert key == f"sha256/{digest[:2]}/{digest[2:]}"
    assert store.load_blob(key) == b"payload"
    assert (tmp_path / "blobs" / key).read_bytes() == b"payload"
