"""Routes for the office's reach: sources, MCP, accounts, connections, providers."""

import base64
import hashlib
import shutil
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app import accounts, provider_balance, provider_cards
from app.api.auth import OwnerIdentity
from app.api.routers.deps import RouterDeps
from app.api.routers.shared import source_view
from app.api.routes import (
    UPLOAD_MAX_BYTES,
    build_indexer,
    found,
    list_mcp_tools,
    safe_relative_path,
)
from app.api.schemas import (
    CalendarSignInStart,
    ConnectionTest,
    CredentialStore,
    DocumentUpload,
    FolderUpload,
    McpBulkDecision,
    McpServerCreate,
    McpToolDecision,
    ModelConnectionCreate,
    ModelConnectionUpdate,
    ModelConnectionView,
    ProviderBackup,
    ProviderModelsQuery,
    SourceCreate,
    SourceView,
    UploadSourceCreate,
)
from app.calendars.store import CalendarRejected
from app.credentials import (
    SECRET_PREFIX,
    credential_present,
    open_secret_store,
    resolve_credential,
    secret_name_for_connection,
)
from app.db.models import KnowledgeSource, McpServer, ModelConnection, ProjectSource, SourceDocument
from app.mcp import catalog as mcp_catalog
from app.mcp import oauth as mcp_oauth
from app.mcp import store as mcp_store
from app.mcp.client import McpClient, McpError
from app.mcp.oauth import OAuthError
from app.model_catalog import describe_all, models_for_provider, price_map
from app.office_settings import effective, set_backup
from app.providers import (
    PRESETS,
    ProviderFailure,
    adapter_for,
    check_connection,
    likely_chat_models,
    preset_for,
)
from app.secret_store import SecretStoreError
from app.sources.client import SourceError
from app.sources.reader import STAGING


def connection_view(d: RouterDeps, connection: ModelConnection) -> dict[str, Any]:
    return {
        "id": connection.id,
        "provider": connection.provider,
        "provider_name": PRESETS[connection.provider].name
        if connection.provider in PRESETS
        else connection.provider,
        "base_url": connection.base_url,
        "display_name": connection.display_name,
        "model_id": connection.model_id,
        "status": connection.status,
        "capabilities": connection.capabilities_json or [],
        "credential_configured": bool(connection.credential_ref),
        "last_checked_at": connection.last_checked_at,
        "last_error": connection.last_error,
        "last_test": (connection.metadata_json or {}).get("last_test"),
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


async def _model_listing(
    d: RouterDeps, provider: str, models: list[str], source: str = "provider"
) -> dict[str, Any]:
    """Model names with what each costs and can do, where that is published."""
    catalogue = await price_map(d.settings.workspace_root)
    chat = set(likely_chat_models(models))
    described = describe_all(catalogue, provider, sorted(models))
    for entry in described:
        entry["chat"] = entry["model"] in chat
    return {
        "models": sorted(models),
        "chat_models": sorted(chat),
        "details": described,
        "prices_known": any(entry["known"] for entry in described),
        # "provider": what this key may use. "catalogue": what the provider
        # publishes, which an account may not be entitled to in full.
        "source": source,
    }


async def run_index(d: RouterDeps, source_id: UUID, root: str) -> dict[str, Any]:
    """Ask the sources service for an index and replace the documents with it."""
    try:
        result = await build_indexer(d.settings).index(root)
    except SourceError as error:
        result = {"documents": [], "skipped": [], "error": error.code}
    now = datetime.now(UTC)
    with d.session_factory() as session, session.begin():
        source = found(session.get(KnowledgeSource, source_id), "Source")
        if result.get("error"):
            source.status = "FAILED"
            source.last_error = str(result["error"])
        else:
            session.execute(delete(SourceDocument).where(SourceDocument.source_id == source_id))
            for item in result["documents"]:
                session.add(
                    SourceDocument(
                        source_id=source_id,
                        path=item["path"],
                        sha256=item["sha256"],
                        size_bytes=item["size_bytes"],
                        modified_at=datetime.fromisoformat(item["modified_at"])
                        if item.get("modified_at")
                        else None,
                        text=item["text"],
                    )
                )
            source.document_count = len(result["documents"])
            source.skipped_json = list(result.get("skipped") or [])
            source.status = "INDEXED" if result["documents"] else "EMPTY"
            source.last_error = None
            source.indexed_at = now
        session.flush()
        session.refresh(source)
        return source_view(d, source)


async def _list_mcp_tools(d: RouterDeps, server_id: UUID) -> dict[str, Any]:
    return await list_mcp_tools(d.session_factory, d.settings, server_id)


def add_reach_routes(router: APIRouter, d: RouterDeps) -> None:
    Owner = Annotated[OwnerIdentity, Depends(d.require_owner)]

    @router.get("/connections", response_model=list[ModelConnectionView])
    def list_connections() -> list[dict[str, Any]]:
        with d.session_factory() as session:
            rows = session.scalars(
                select(ModelConnection).order_by(ModelConnection.created_at, ModelConnection.id)
            )
            return [connection_view(d, row) for row in rows]

    @router.post("/connections", response_model=ModelConnectionView, status_code=201)
    def create_connection(body: ModelConnectionCreate) -> dict[str, Any]:
        """Register provider metadata without accepting a raw API key.

        A credential is either an ``env:NAME`` reference or, when omitted, a
        key stored afterwards through ``PUT /connections/{id}/credential``.
        Registration does not claim provider connectivity.
        """
        try:
            preset = preset_for(body.provider)
        except ProviderFailure:
            raise HTTPException(status_code=422, detail="provider_unsupported") from None
        base_url = body.base_url or preset.base_url
        if preset.needs_base_url and not base_url:
            raise HTTPException(status_code=422, detail="base_url_required") from None
        connection = ModelConnection(
            provider=body.provider,
            display_name=body.display_name,
            model_id=body.model_id,
            base_url=base_url,
            credential_ref=body.credential_ref or "",
            capabilities_json=body.capabilities,
            status="CONFIGURED" if body.credential_ref else "CREDENTIAL_MISSING",
        )
        try:
            with d.session_factory() as session, session.begin():
                session.add(connection)
                session.flush()
                session.refresh(connection)
                return connection_view(d, connection)
        except IntegrityError:
            raise HTTPException(status_code=409, detail="connection_name_already_exists") from None

    @router.get("/providers")
    def providers() -> list[dict[str, Any]]:
        """Every provider the office can connect to, and how to reach it."""
        return [
            {
                "id": preset.id,
                "name": preset.name,
                "about": preset.about,
                "base_url": preset.base_url,
                "examples": list(preset.examples),
                "needs_base_url": preset.needs_base_url,
                "needs_key": preset.needs_key,
            }
            for preset in PRESETS.values()
        ]

    @router.post("/providers/{provider}/models")
    async def provider_models(provider: str, body: ProviderModelsQuery) -> dict[str, Any]:
        """What this provider offers, asked with a key that is not stored.

        The catalogue's example models age; the provider's own list does not.
        Listing models is not a billable request.
        """
        try:
            preset = preset_for(provider)
        except ProviderFailure:
            raise HTTPException(status_code=422, detail="provider_unsupported") from None
        key = body.api_key.get_secret_value() if body.api_key else ""
        base_url = body.base_url or preset.base_url
        if not key and preset.needs_key:
            # No key yet: show what the provider publishes, from the price list.
            catalogue = await price_map(d.settings.workspace_root)
            listed = models_for_provider(catalogue, provider)
            if not listed:
                raise HTTPException(status_code=409, detail="credential_missing")
            return await _model_listing(d, provider, listed, source="catalogue")
        if preset.needs_base_url and not base_url:
            raise HTTPException(status_code=422, detail="base_url_required")
        try:
            models = await adapter_for(provider, base_url).list_models(key or "none")
        except ProviderFailure as failure:
            raise HTTPException(status_code=502, detail=failure.code) from None
        return await _model_listing(d, provider, models)

    @router.get("/connections/{connection_id}/models")
    async def connection_models(connection_id: UUID) -> dict[str, Any]:
        """Ask the provider which models this key can use. Not billable."""
        with d.session_factory() as session:
            connection = found(session.get(ModelConnection, connection_id), "Connection")
            provider, reference, base_url = (
                connection.provider,
                connection.credential_ref,
                connection.base_url,
            )
        credential = ""
        if reference:
            try:
                credential = resolve_credential(reference, d.settings)
            except SecretStoreError as error:
                raise HTTPException(status_code=409, detail=error.code) from None
        elif preset_for(provider).needs_key:
            raise HTTPException(status_code=409, detail="credential_missing")
        try:
            models = await adapter_for(provider, base_url).list_models(credential or "none")
        except ProviderFailure as failure:
            raise HTTPException(status_code=502, detail=failure.code) from None
        return await _model_listing(d, provider, models)

    @router.patch("/connections/{connection_id}", response_model=ModelConnectionView)
    def update_connection(connection_id: UUID, body: ModelConnectionUpdate) -> dict[str, Any]:
        """Change the model, name or address. The key and history are kept."""
        try:
            with d.session_factory() as session, session.begin():
                connection = found(session.get(ModelConnection, connection_id), "Connection")
                changed = body.model_dump(exclude_unset=True)
                for field, value in changed.items():
                    if value is not None:
                        setattr(connection, field, value)
                if {"model_id", "base_url"} & set(changed):
                    # The provider has not confirmed this combination yet.
                    connection.status = (
                        "PROVIDER_TEST_PENDING"
                        if connection.credential_ref
                        else "CREDENTIAL_MISSING"
                    )
                    connection.last_error = "provider_network_test_not_run"
                session.flush()
                session.refresh(connection)
                return connection_view(d, connection)
        except IntegrityError:
            raise HTTPException(status_code=409, detail="connection_name_already_exists") from None

    @router.put("/connections/{connection_id}/credential", response_model=ModelConnectionView)
    def store_connection_credential(connection_id: UUID, body: CredentialStore) -> dict[str, Any]:
        """Encrypt the key into the local secret store; the database keeps a reference."""
        with d.session_factory() as session, session.begin():
            connection = found(session.get(ModelConnection, connection_id), "Connection")
            name = secret_name_for_connection(connection.id)
            try:
                open_secret_store(d.settings).put(name, body.secret.get_secret_value())
            except SecretStoreError as error:
                raise HTTPException(status_code=503, detail=error.code) from None
            connection.credential_ref = f"{SECRET_PREFIX}{name}"
            connection.status = "PROVIDER_TEST_PENDING"
            connection.last_error = "provider_network_test_not_run"
            session.flush()
            session.refresh(connection)
            return connection_view(d, connection)

    @router.delete("/connections/{connection_id}/credential", response_model=ModelConnectionView)
    def remove_connection_credential(connection_id: UUID) -> dict[str, Any]:
        """Revoke locally: delete the stored key and clear the reference."""
        with d.session_factory() as session, session.begin():
            connection = found(session.get(ModelConnection, connection_id), "Connection")
            if connection.credential_ref.startswith(SECRET_PREFIX):
                try:
                    open_secret_store(d.settings).delete(
                        connection.credential_ref.removeprefix(SECRET_PREFIX)
                    )
                except SecretStoreError as error:
                    raise HTTPException(status_code=503, detail=error.code) from None
            connection.credential_ref = ""
            connection.status = "CREDENTIAL_MISSING"
            connection.last_error = None
            session.flush()
            session.refresh(connection)
            return connection_view(d, connection)

    @router.post(
        "/connections/{connection_id}/check-credential", response_model=ModelConnectionView
    )
    def check_connection_credential(connection_id: UUID) -> dict[str, Any]:
        """Check only local credential presence; no network/provider claim is made."""
        with d.session_factory() as session, session.begin():
            connection = found(session.get(ModelConnection, connection_id), "Connection")
            connection.last_checked_at = datetime.now(UTC)
            try:
                present = credential_present(connection.credential_ref, d.settings)
            except SecretStoreError as error:
                connection.status = "CREDENTIAL_STORE_UNAVAILABLE"
                connection.last_error = error.code
            else:
                if present:
                    connection.status = "PROVIDER_TEST_PENDING"
                    connection.last_error = "provider_network_test_not_run"
                else:
                    connection.status = "CREDENTIAL_MISSING"
                    connection.last_error = "credential_reference_not_available"
            session.flush()
            session.refresh(connection)
            return connection_view(d, connection)

    @router.post("/connections/{connection_id}/test", response_model=ModelConnectionView)
    async def test_connection(connection_id: UUID, body: ConnectionTest) -> dict[str, Any]:
        """Check the credential and model with the provider itself.

        Model discovery is not billed. ``billable: true`` additionally sends one
        minimal request, whose usage is recorded. Never falls back elsewhere.
        """
        with d.session_factory() as session:
            connection = found(session.get(ModelConnection, connection_id), "Connection")
            provider, model_id, reference, base_url = (
                connection.provider,
                connection.model_id,
                connection.credential_ref,
                connection.base_url,
            )
        if not reference:
            raise HTTPException(status_code=409, detail="credential_missing")
        try:
            credential = resolve_credential(reference, d.settings)
        except SecretStoreError as error:
            detail = "credential_missing" if error.code == "secret_not_found" else error.code
            raise HTTPException(
                status_code=409 if detail == "credential_missing" else 503, detail=detail
            ) from None
        result = await check_connection(
            adapter_for(provider, base_url), credential, model_id, billable=body.billable
        )
        checked_at = datetime.now(UTC)
        with d.session_factory() as session, session.begin():
            connection = found(session.get(ModelConnection, connection_id), "Connection")
            connection.status = result.status
            connection.last_error = result.error
            connection.last_checked_at = checked_at
            connection.metadata_json = {
                **(connection.metadata_json or {}),
                "last_test": {
                    "billable": result.billable,
                    "latency_ms": result.latency_ms,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "checked_at": checked_at.isoformat(),
                },
            }
            session.flush()
            session.refresh(connection)
            return connection_view(d, connection)

    @router.post("/sources/{source_id}/files", status_code=201)
    async def add_files(source_id: UUID, body: FolderUpload) -> dict[str, Any]:
        """Take part of a folder the owner chose in their browser.

        The bytes are staged in the exchange and read by the `sources` service,
        which has no network and no credentials; nothing is parsed here. The
        staged copy is deleted as soon as it has been read. A browser sends a
        folder in batches: the first replaces the source's documents, the rest
        add to them.
        """
        with d.session_factory() as session:
            source = found(session.get(KnowledgeSource, source_id), "Source")
            if source.kind != "upload":
                raise HTTPException(status_code=409, detail="not_an_upload_source")
        staging = d.settings.sources_exchange / STAGING
        directory = uuid4().hex
        target = staging / directory
        try:
            total = 0
            for item in body.files:
                relative = safe_relative_path(item.path)
                if relative is None:
                    continue  # An unusable name is not evidence; skip it.
                try:
                    data = base64.b64decode(item.content, validate=True)
                except (ValueError, TypeError):
                    raise HTTPException(status_code=422, detail="file_not_base64") from None
                total += len(data)
                if total > UPLOAD_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="upload_too_large")
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            try:
                result = await build_indexer(d.settings).index_staged(directory)
            except SourceError as error:
                raise HTTPException(status_code=503, detail=error.code) from None
        finally:
            shutil.rmtree(target, ignore_errors=True)
        if result.get("error"):
            raise HTTPException(status_code=422, detail=str(result["error"]))
        now = datetime.now(UTC)
        with d.session_factory() as session, session.begin():
            source = found(session.get(KnowledgeSource, source_id), "Source")
            if body.replace:
                session.execute(delete(SourceDocument).where(SourceDocument.source_id == source_id))
            for item in result["documents"]:
                session.execute(
                    delete(SourceDocument).where(
                        SourceDocument.source_id == source_id,
                        SourceDocument.path == item["path"],
                    )
                )
                session.add(
                    SourceDocument(
                        source_id=source_id,
                        path=item["path"],
                        sha256=item["sha256"],
                        size_bytes=item["size_bytes"],
                        modified_at=datetime.fromisoformat(item["modified_at"])
                        if item.get("modified_at")
                        else None,
                        text=item["text"],
                    )
                )
            session.flush()
            skipped = list(result.get("skipped") or [])
            source.skipped_json = skipped if body.replace else (source.skipped_json or []) + skipped
            source.document_count = (
                session.scalar(select(func.count()).where(SourceDocument.source_id == source_id))
                or 0
            )
            source.status = "INDEXED" if source.document_count else "EMPTY"
            source.last_error = None
            source.indexed_at = now
            session.flush()
            session.refresh(source)
            return source_view(d, source)

    @router.get("/sources/folders")
    async def source_folders() -> dict[str, list[str]]:
        """Subfolders of the shared folder, for choosing a source root."""
        try:
            return {"folders": await build_indexer(d.settings).folders()}
        except SourceError as error:
            raise HTTPException(status_code=503, detail=error.code) from None

    @router.get("/sources", response_model=list[SourceView])
    def list_sources() -> list[dict[str, Any]]:
        with d.session_factory() as session:
            rows = session.scalars(select(KnowledgeSource).order_by(KnowledgeSource.name))
            return [source_view(d, row) for row in rows]

    @router.post("/sources", response_model=SourceView, status_code=201)
    async def create_folder_source(body: SourceCreate) -> dict[str, Any]:
        """Register a subfolder of the read-only mount and index it now."""
        try:
            with d.session_factory() as session, session.begin():
                source = KnowledgeSource(name=body.name, kind="folder", root=body.root)
                session.add(source)
                session.flush()
                source_id = source.id
        except IntegrityError:
            raise HTTPException(status_code=409, detail="source_name_already_exists") from None
        return await run_index(d, source_id, body.root)

    @router.post("/sources/upload", response_model=SourceView, status_code=201)
    def create_upload_source(body: UploadSourceCreate) -> dict[str, Any]:
        """The no-folder fallback: documents are added as text through the API."""
        try:
            with d.session_factory() as session, session.begin():
                source = KnowledgeSource(name=body.name, kind="upload", status="EMPTY")
                session.add(source)
                session.flush()
                session.refresh(source)
                return source_view(d, source)
        except IntegrityError:
            raise HTTPException(status_code=409, detail="source_name_already_exists") from None

    @router.get("/sources/{source_id}", response_model=SourceView)
    def get_source(source_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            return source_view(d, found(session.get(KnowledgeSource, source_id), "Source"))

    @router.post("/sources/{source_id}/reindex", response_model=SourceView)
    async def reindex_source(source_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            source = found(session.get(KnowledgeSource, source_id), "Source")
            kind, root = source.kind, source.root
        if kind != "folder" or not root:
            raise HTTPException(status_code=409, detail="not_a_folder_source")
        return await run_index(d, source_id, root)

    @router.post("/sources/{source_id}/documents", status_code=201)
    def add_document(source_id: UUID, body: DocumentUpload) -> dict[str, Any]:
        data = body.text.encode()
        with d.session_factory() as session, session.begin():
            source = found(session.get(KnowledgeSource, source_id), "Source")
            if source.kind != "upload":
                raise HTTPException(status_code=409, detail="not_an_upload_source")
            session.execute(
                delete(SourceDocument).where(
                    SourceDocument.source_id == source_id, SourceDocument.path == body.path
                )
            )
            document = SourceDocument(
                source_id=source_id,
                path=body.path,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                text=body.text,
            )
            session.add(document)
            session.flush()
            source.document_count = (
                session.scalar(select(func.count()).where(SourceDocument.source_id == source_id))
                or 0
            )
            source.status = "INDEXED"
            source.indexed_at = datetime.now(UTC)
            return {"id": str(document.id), "path": document.path, "sha256": document.sha256}

    @router.get("/sources/{source_id}/documents")
    def list_documents(source_id: UUID) -> list[dict[str, Any]]:
        with d.session_factory() as session:
            found(session.get(KnowledgeSource, source_id), "Source")
            rows = session.execute(
                select(
                    SourceDocument.id,
                    SourceDocument.path,
                    SourceDocument.size_bytes,
                    SourceDocument.sha256,
                    SourceDocument.modified_at,
                )
                .where(SourceDocument.source_id == source_id)
                .order_by(SourceDocument.path)
            )
            return [
                {
                    "id": str(row.id),
                    "path": row.path,
                    "size_bytes": row.size_bytes,
                    "sha256": row.sha256,
                    "modified_at": row.modified_at,
                }
                for row in rows
            ]

    @router.get("/sources/{source_id}/documents/{document_id}")
    def read_document(source_id: UUID, document_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            document = found(session.get(SourceDocument, document_id), "Document")
            if document.source_id != source_id:
                raise HTTPException(status_code=404, detail="Document not found")
            return {"id": str(document.id), "path": document.path, "text": document.text}

    @router.delete("/sources/{source_id}", status_code=204)
    def delete_source(source_id: UUID) -> None:
        """Remove the source, its extracted text and every grant. Files are untouched."""
        with d.session_factory() as session, session.begin():
            source = found(session.get(KnowledgeSource, source_id), "Source")
            session.execute(delete(ProjectSource).where(ProjectSource.source_id == source_id))
            session.execute(delete(SourceDocument).where(SourceDocument.source_id == source_id))
            session.delete(source)

    @router.post("/mcp/servers", status_code=201)
    async def add_mcp_server(body: McpServerCreate, owner: Owner) -> dict[str, Any]:
        """Record a server and ask what it offers. Nothing becomes callable.

        The handshake happens here so the owner learns at once whether the
        address is reachable, rather than discovering it inside a task.
        """
        checked = str(body.url)
        with d.session_factory() as session:
            taken = session.scalar(select(McpServer.id).where(McpServer.name == body.name))
        if taken is not None:
            # The name is how the owner and the agents tell servers apart.
            raise HTTPException(status_code=409, detail="mcp_name_taken")
        with d.session_factory() as session, session.begin():
            server = McpServer(name=body.name, url=checked, status=mcp_store.UNREACHED)
            session.add(server)
            session.flush()
            server_id = server.id
            if body.secret is not None:
                reference = f"mcp-{server_id}"
                open_secret_store(d.settings).put(reference, body.secret.get_secret_value())
                server.credential_ref = f"{SECRET_PREFIX}{reference}"
        return await _list_mcp_tools(d, server_id)

    @router.post("/mcp/servers/{server_id}/list")
    async def refresh_mcp_server(server_id: UUID, owner: Owner) -> dict[str, Any]:
        """Ask a server again what it offers."""
        return await _list_mcp_tools(d, server_id)

    @router.post("/mcp/servers/{server_id}/sign-in")
    async def sign_in_to_mcp_server(
        server_id: UUID, request: Request, back: str = ""
    ) -> dict[str, str]:
        """Begin signing in with the owner's account: the vendor's page to open.

        The vendor sends the owner's browser back to this office's callback, on
        the address the owner is using now, with a code the office redeems once.
        """
        with d.session_factory() as session:
            url = found(session.get(McpServer, server_id), "Server").url
        allow_list = mcp_store.allow_hosts(d.settings)
        challenge = ""
        try:
            # Asked without a key, the server says where its sign-in begins.
            await McpClient().offering(url, None, allow_list)
        except McpError as asked:
            challenge = asked.challenge
        redirect_uri = str(request.base_url).rstrip("/") + "/mcp/oauth/callback"
        try:
            found_server = await mcp_oauth.discover(url, challenge, allow_list)
            registration = await mcp_oauth.register(found_server, redirect_uri, allow_list)
        except OAuthError as refused:
            raise HTTPException(status_code=409, detail=f"mcp_{refused.code}") from None
        page, verifier, state = mcp_oauth.authorize_url(found_server, registration, redirect_uri)
        try:
            d.sign_ins.start(
                {
                    "server_id": str(server_id),
                    **mcp_oauth.pending(found_server, registration),
                    "verifier": verifier,
                    "redirect_uri": redirect_uri,
                    # Started from the Accounts page, it returns there.
                    "back": "accounts" if back == "accounts" else "tools",
                },
                state,
            )
        except SecretStoreError as unavailable:
            # The tokens would need the store too: say so before the vendor's page.
            raise HTTPException(status_code=409, detail=unavailable.code) from None
        return {"authorize_url": page}

    @router.post("/mcp/servers/{server_id}/decide-undecided")
    def decide_undecided_tools(
        server_id: UUID, body: McpBulkDecision, owner: Owner
    ) -> dict[str, Any]:
        """The owner's one decision for every tool not yet decided."""
        with d.session_factory() as session, session.begin():
            try:
                decided = mcp_store.decide_undecided(
                    session, server_id, body.read_only, body.others, owner.name
                )
            except LookupError as missing:
                raise HTTPException(status_code=404, detail=str(missing.args[0])) from None
            return {
                **mcp_store.view(found(session.get(McpServer, server_id), "Server")),
                "decided": decided,
            }

    @router.post("/mcp/servers/{server_id}/tools/{tool_name}/decision")
    def decide_mcp_tool(
        server_id: UUID, tool_name: str, body: McpToolDecision, owner: Owner
    ) -> dict[str, Any]:
        """What the owner decided about one tool. Nothing is callable before this.

        The decision is bound to the tool as it stands now: a tool whose
        description or schema changes afterwards is unreviewed again.
        """
        with d.session_factory() as session, session.begin():
            try:
                mcp_store.record_decision(
                    session,
                    server_id,
                    tool_name,
                    body.stance,
                    owner.name,
                    body.acts_as,
                    body.price_usd,
                )
            except LookupError as missing:
                raise HTTPException(status_code=404, detail=str(missing.args[0])) from None
            except ValueError as unknown:
                raise HTTPException(status_code=422, detail=str(unknown)) from None
            return mcp_store.view(found(session.get(McpServer, server_id), "Server"))

    # ---- The owner's accounts, in one place ----
    @router.get("/accounts")
    def account_page() -> dict[str, Any]:
        try:
            store: Any = open_secret_store(d.settings)
        except SecretStoreError:
            store = None
        with d.session_factory() as session:
            browser_on = bool(effective(session, d.settings)["browser_on"])
            return accounts.page(session, store, d.calendar_service.apps(), browser_on)

    @router.post("/accounts/{key}/connect")
    def connect_account(key: str, body: CalendarSignInStart) -> dict[str, str]:
        """Google's or Microsoft's consent page for the owner's mail and files."""
        try:
            return {"url": accounts.begin(d.calendar_service, key, body.origin)}
        except accounts.AccountError as refused:
            raise HTTPException(status_code=404, detail=refused.code) from None
        except CalendarRejected as rejected:
            raise HTTPException(status_code=422, detail=rejected.code) from None

    @router.put("/mcp/servers/{server_id}/no-project", status_code=204)
    def open_mcp_to_no_project(server_id: UUID, owner: Owner) -> None:
        """Let discussions with no project use this server's reviewed tools."""
        with d.session_factory() as session, session.begin():
            found(session.get(McpServer, server_id), "Server").no_project_discussions = True

    @router.delete("/mcp/servers/{server_id}/no-project", status_code=204)
    def close_mcp_to_no_project(server_id: UUID, owner: Owner) -> None:
        """Take it away. Its tools stop being offered to them at once."""
        with d.session_factory() as session, session.begin():
            found(session.get(McpServer, server_id), "Server").no_project_discussions = False

    @router.get("/mcp/catalog")
    def mcp_server_catalog() -> dict[str, Any]:
        """A reviewed list of tool servers to add, by category. Adding one is
        adding by hand with the form filled in; nothing is installed from here."""
        with d.session_factory() as session:
            return mcp_catalog.listed(session)

    @router.get("/mcp/servers")
    def list_mcp_servers() -> list[dict[str, Any]]:
        """Every server the owner has added, and what each last offered."""
        with d.session_factory() as session:
            return mcp_store.listed(session)

    @router.delete("/mcp/servers/{server_id}", status_code=204)
    def remove_mcp_server(server_id: UUID, owner: Owner) -> None:
        """Forget a server, and its credential with it."""
        with d.session_factory() as session, session.begin():
            server = found(session.get(McpServer, server_id), "Server")
            if server.credential_ref and server.credential_ref.startswith(SECRET_PREFIX):
                open_secret_store(d.settings).delete(
                    server.credential_ref.removeprefix(SECRET_PREFIX)
                )
            session.delete(server)

    @router.get("/providers/cards")
    async def provider_cards_view() -> dict[str, Any]:
        """One card per provider: who uses it, what it has left, its backup."""
        balances = await provider_balance.balances(
            d.session_factory, d.settings, mcp_store.allow_hosts(d.settings)
        )
        with d.session_factory() as session:
            return provider_cards.cards(session, d.settings, balances)

    @router.get("/providers/usage")
    def provider_usage(days: int = 7) -> dict[str, Any]:
        """Calls, tokens, cost, errors and failovers per provider."""
        with d.session_factory() as session:
            return provider_cards.usage(session, max(1, min(days, 90)))

    @router.put("/providers/{key}/backup")
    def choose_backup(key: str, body: ProviderBackup) -> dict[str, Any]:
        """Choose the provider a failing one's calls go to, or none."""
        with d.session_factory() as session, session.begin():
            if not provider_cards.valid_key(session, key):
                raise HTTPException(status_code=404, detail="Provider not found")
            if body.backup is not None:
                if body.backup == key:
                    raise HTTPException(status_code=422, detail="backup_is_itself")
                if not provider_cards.valid_key(session, body.backup):
                    raise HTTPException(status_code=422, detail="backup_not_found")
            set_backup(session, key, body.backup, body.come_back)
        return {"key": key, "backup": body.backup, "come_back": body.come_back}

    @router.get("/connections/balances")
    async def connection_balances() -> list[dict[str, Any]]:
        """What is left on each provider account, where the provider says."""
        return await provider_balance.balances(
            d.session_factory, d.settings, mcp_store.allow_hosts(d.settings)
        )
