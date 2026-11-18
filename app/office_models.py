"""The office's model for each role, called directly.

Code asks for a role's model by its alias (`settings.model_aliases`), as it did
when a LiteLLM gateway answered those aliases. The gateway is retired (22 Oct,
security): an alias now names a role, and the role's model is the connection
the owner chose for it (Settings → roles), called with that connection's own
key. A role with no connection fails its call with a code; nothing falls back
to another model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.connection_completer import completer_for_entry
from app.gateway.client import Completion, GatewayError
from app.gateway.direct import Completer, sees_pictures
from app.office_settings import role_models as office_roles
from app.project_settings import role_models as project_roles

Entries = Callable[[], dict[str, dict[str, Any]]]


class OfficeModels:
    """Answers a role alias with that role's chosen connection."""

    def __init__(
        self,
        settings: Settings,
        entries: Entries,
        completer_for: Callable[[dict[str, Any]], Completer],
    ) -> None:
        self.settings = settings
        self.entries = entries
        self.completer_for = completer_for

    async def __aenter__(self) -> OfficeModels:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def role_of(self, model_alias: str) -> str:
        role = next((r for r, a in self.settings.model_aliases.items() if a == model_alias), None)
        if role is None:
            raise GatewayError("unknown_model_alias")
        return role

    async def model_for(self, model_alias: str) -> Completer:
        """The completer for the alias's role; refused when the role has no model."""
        role = self.role_of(model_alias)
        entry = (await asyncio.to_thread(self.entries)).get(role)
        if entry is None:
            raise GatewayError("role_has_no_model")
        return self.completer_for(entry)

    async def complete(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        model = await self.model_for(model_alias)
        return await model.complete(model_alias, messages, max_tokens, schema, tools)

    async def sees(self, model_alias: str) -> bool:
        """Whether the alias's role model reads pictures; no model sees nothing."""
        try:
            return sees_pictures(await self.model_for(model_alias))
        except GatewayError:
            return False


async def can_see(completer: Any, model_alias: str) -> bool:
    """Whether the model behind this completer and alias reads pictures."""
    if isinstance(completer, OfficeModels):
        return await completer.sees(model_alias)
    return sees_pictures(completer)


def office_models(
    settings: Settings,
    session_factory: sessionmaker[Session] | None,
    project_id: UUID | None = None,
) -> OfficeModels:
    """The office's role models, read fresh at each call: a project's own choices
    over the office's when `project_id` is given."""

    def entries() -> dict[str, dict[str, Any]]:
        if session_factory is None:
            return {}
        with session_factory() as session:
            if project_id is not None:
                return dict(project_roles(session, settings, project_id))
            return office_roles(session, settings)

    return OfficeModels(
        settings, entries, lambda entry: completer_for_entry(entry, settings, session_factory)
    )
