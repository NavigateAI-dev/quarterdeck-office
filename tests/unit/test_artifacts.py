from pathlib import Path
from uuid import uuid4

import pytest

from app.artifacts import ArtifactMetadata, ArtifactStore


def test_artifact_write_is_atomic_and_confined(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    metadata = ArtifactMetadata(project_id=uuid4(), type="research_report", created_by="researcher")
    path = store.write(metadata, "report")
    assert path.read_text() == "report"
    assert store.metadata_json(metadata)["type"] == "research_report"
    with pytest.raises(FileExistsError):
        store.write(metadata, "duplicate")


def test_versions_are_immutable_and_listed_in_order(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    project_id, artifact_id = uuid4(), uuid4()
    for version, body in ((1, "first"), (2, "second")):
        store.write(
            ArtifactMetadata(
                artifact_id=artifact_id,
                project_id=project_id,
                type="research_report",
                created_by="researcher",
                version=version,
            ),
            body,
        )
    assert store.versions(project_id, artifact_id) == [1, 2]
    assert store.read(project_id, artifact_id, 1) == "first"


def test_another_project_cannot_read_the_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    metadata = ArtifactMetadata(project_id=uuid4(), type="research_report", created_by="researcher")
    store.write(metadata, "confidential")
    other_project = uuid4()
    with pytest.raises(FileNotFoundError):
        store.read(other_project, metadata.artifact_id, 1)
    assert store.versions(other_project, metadata.artifact_id) == []
