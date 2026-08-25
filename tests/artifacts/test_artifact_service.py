from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pickel.artifacts.artifact import ArtifactReference
from pickel.artifacts.artifact_service import ArtifactIntegrityError, ArtifactService
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
from pickel.artifacts.filesystem_blob_store import FilesystemBlobStore
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore


@pytest.fixture(params=["memory", "sqlite"])
def artifact_store(request, tmp_path: Path):
    return (
        InMemoryRuntimeStore()
        if request.param == "memory"
        else SQLiteRuntimeStore(tmp_path / "runtime.db")
    )


def test_create_artifact_keeps_reference_metadata_out_of_artifact(
    artifact_store,
) -> None:
    blobs = InMemoryBlobStore()
    service = ArtifactService(artifact_store=artifact_store, blob_store=blobs)
    reference = service.create_artifact(
        data=b"image-bytes", media_type="image/png", display_name="chart.png"
    )

    assert reference.artifact_id.startswith("artifact_")
    assert reference.display_name == "chart.png"
    assert service.load_artifact_bytes(reference) == b"image-bytes"
    metadata = artifact_store.load_artifact(reference.artifact_id)
    assert metadata is not None
    assert set(metadata.to_dict()) == {"artifact_id", "size_bytes", "created_at"}


def test_create_same_content_is_idempotent(artifact_store) -> None:
    service = ArtifactService(
        artifact_store=artifact_store, blob_store=InMemoryBlobStore()
    )
    first = service.create_artifact(
        data=b"same", media_type="audio/mpeg", display_name="first.mp3"
    )
    second = service.create_artifact(
        data=b"same", media_type="audio/mpeg", display_name="second.mp3"
    )
    assert first.artifact_id == second.artifact_id
    assert second.media_type == "audio/mpeg"
    assert second.display_name == "second.mp3"


def test_recreating_content_restores_a_missing_blob(artifact_store) -> None:
    blobs = InMemoryBlobStore()
    service = ArtifactService(artifact_store=artifact_store, blob_store=blobs)
    reference = service.create_artifact(data=b"same", media_type="text/plain")
    blobs.delete_blob(reference.artifact_id)

    recreated = service.create_artifact(data=b"same", media_type="text/plain")

    assert recreated.artifact_id == reference.artifact_id
    assert service.load_artifact_bytes(recreated) == b"same"


def test_load_rejects_missing_artifact_metadata(artifact_store) -> None:
    service = ArtifactService(
        artifact_store=artifact_store, blob_store=InMemoryBlobStore()
    )
    reference = ArtifactReference("artifact_" + "0" * 64, "image/png")
    with pytest.raises(ArtifactIntegrityError, match="元数据不存在"):
        service.load_artifact_bytes(reference)


def test_filesystem_blob_store_is_addressed_only_by_artifact_id(
    tmp_path: Path,
) -> None:
    data = b"blob"
    digest = hashlib.sha256(data).hexdigest()
    artifact_id = f"artifact_{digest}"
    store = FilesystemBlobStore(tmp_path)

    store.put_blob(artifact_id=artifact_id, data=data)
    assert store.load_blob(artifact_id) == data
    assert (tmp_path / "sha256" / digest[:2] / digest[2:]).read_bytes() == data

    with pytest.raises(ValueError, match="artifact_id"):
        store.put_blob(artifact_id="artifact_" + "0" * 64, data=data)

    store.delete_blob(artifact_id)
    with pytest.raises(LookupError):
        store.load_blob(artifact_id)
