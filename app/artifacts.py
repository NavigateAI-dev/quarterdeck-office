import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ArtifactMetadata(BaseModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    task_id: UUID | None = None
    run_id: UUID | None = None
    type: str = Field(min_length=1, max_length=64)
    created_by: str = Field(min_length=1, max_length=128)
    version: int = Field(default=1, ge=1)


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: UUID, artifact_id: UUID, version: int) -> Path:
        """Resolve a project-scoped artifact path confined to the workspace root."""
        if version < 1:
            raise ValueError("Artifact versions start at 1")
        relative = Path(str(project_id)) / str(artifact_id) / f"v{version}.md"
        destination = (self.root / relative).resolve()
        if self.root not in destination.parents:
            raise ValueError("Artifact path escapes workspace root")
        return destination

    def path_of(self, metadata: ArtifactMetadata) -> Path:
        """Resolve an artifact's path without writing it."""
        return self._path(metadata.project_id, metadata.artifact_id, metadata.version)

    def read(self, project_id: UUID, artifact_id: UUID, version: int) -> str:
        """Read one artifact version. A different project cannot reach it."""
        return self._path(project_id, artifact_id, version).read_text(encoding="utf-8")

    def versions(self, project_id: UUID, artifact_id: UUID) -> list[int]:
        directory = self._path(project_id, artifact_id, 1).parent
        if not directory.is_dir():
            return []
        return sorted(
            int(path.stem[1:]) for path in directory.glob("v*.md") if path.stem[1:].isdigit()
        )

    def write(self, metadata: ArtifactMetadata, body: str) -> Path:
        destination = self._path(metadata.project_id, metadata.artifact_id, metadata.version)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(destination)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            temporary.write_text(body, encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def metadata_json(metadata: ArtifactMetadata) -> dict[str, object]:
        value = json.loads(metadata.model_dump_json())
        if not isinstance(value, dict):
            raise TypeError("artifact metadata must serialize to an object")
        return value
