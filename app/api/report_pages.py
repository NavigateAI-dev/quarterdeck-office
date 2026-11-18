"""A finished answer as a page of its own, at a private link.

Like a build's preview, it is reached by a link rather than the owner's
session, so it can be opened from Slack or passed to a colleague who can reach
the office. The link is an HMAC of the artifact: it cannot be guessed, and it
names one version of one answer only.

The page is the document the office writes for its own HTML download, charts
included, served with a policy that allows no script and no request out: it
can only be read.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy.orm import Session, sessionmaker

from app.artifacts import ArtifactStore
from app.config import Settings
from app.db.models import Artifact, Task
from app.documents import document_for, to_html

PAGE_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)


def page_token(secret: str, artifact_id: UUID | str) -> str:
    return hmac.new(
        secret.encode(), f"report-page:{artifact_id}".encode(), hashlib.sha256
    ).hexdigest()[:32]


def page_path(settings: Settings, artifact_id: UUID | str) -> str | None:
    """The page's address on the office, or None without an owner secret."""
    secret = settings.owner_api_token.get_secret_value() if settings.owner_api_token else ""
    if not secret:
        return None
    return f"/pages/{artifact_id}/{page_token(secret, artifact_id)}"


def build_report_page_router(
    session_factory: sessionmaker[Session], settings: Settings
) -> APIRouter:
    router = APIRouter()
    store = ArtifactStore(settings.workspace_root)

    @router.get("/pages/{artifact_id}/{token}")
    def report_page(artifact_id: UUID, token: str) -> Response:
        secret = settings.owner_api_token.get_secret_value() if settings.owner_api_token else ""
        if not secret or not hmac.compare_digest(token, page_token(secret, artifact_id)):
            raise HTTPException(status_code=404, detail="Not found")
        with session_factory() as session:
            artifact = session.get(Artifact, artifact_id)
            if artifact is None:
                raise HTTPException(status_code=404, detail="Not found")
            task = session.get(Task, artifact.task_id) if artifact.task_id else None
            objective = task.objective if task is not None else None
            project_id, version, kind = artifact.project_id, artifact.version, artifact.type
        try:
            body = store.read(project_id, artifact_id, version)
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="Not found") from None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"text": body}
        title = objective.strip().splitlines()[0][:120] if objective and objective.strip() else None
        fields = payload if isinstance(payload, dict) else {"text": body}
        page = to_html(document_for(kind, fields, title))
        return Response(
            content=page,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Security-Policy": PAGE_POLICY,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex",
            },
        )

    return router
