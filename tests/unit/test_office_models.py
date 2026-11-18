"""Each role alias answered by the connection the owner chose for the role."""

from typing import Any

import pytest

from app.config import Settings
from app.gateway.client import Completion, GatewayError, Usage
from app.office_models import OfficeModels


def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+psycopg://office:secret@localhost/office",
    )


class Model:
    def __init__(self, name: str, sees: bool = False) -> None:
        self.name = name
        self.sees = sees
        self.asked: list[str] = []

    async def complete(self, model_alias: str, messages: list[dict[str, Any]], *args: Any) -> Any:
        self.asked.append(model_alias)
        return Completion(
            model_alias=f"connection:{self.name}",
            model=self.name,
            content="ok",
            usage=Usage(),
            latency_seconds=0.0,
        )


def office(entries: dict[str, dict[str, Any]], models: dict[str, Model]) -> OfficeModels:
    return OfficeModels(settings(), lambda: entries, lambda entry: models[entry["connection_id"]])


async def test_an_alias_is_answered_by_its_role_connection() -> None:
    critic = Model("critic-model")
    models = office({"critic": {"connection_id": "c"}}, {"c": critic})
    completion = await models.complete(settings().critic_model_alias, [])
    assert completion.model == "critic-model"
    assert critic.asked == [settings().critic_model_alias]


async def test_a_role_without_a_connection_fails_with_a_code() -> None:
    models = office({}, {})
    with pytest.raises(GatewayError) as raised:
        await models.complete(settings().chief_model_alias, [])
    assert raised.value.code == "role_has_no_model"


async def test_an_unknown_alias_is_refused() -> None:
    with pytest.raises(GatewayError) as raised:
        await office({}, {}).complete("not-an-alias", [])
    assert raised.value.code == "unknown_model_alias"


async def test_a_role_sees_when_its_connection_does() -> None:
    models = office({"chief": {"connection_id": "c"}}, {"c": Model("m", sees=True)})
    assert await models.sees(settings().chief_model_alias) is True


async def test_a_role_without_a_model_does_not_see() -> None:
    assert await office({}, {}).sees(settings().chief_model_alias) is False


async def test_the_smoke_test_asks_every_role_and_fails_if_one_has_no_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from scripts.smoke_test_models import smoke_test

    class Says(Model):
        async def complete(self, model_alias: str, messages: Any, *args: Any) -> Any:
            completion = await super().complete(model_alias, messages)
            return completion.model_copy(update={"content": "OK"})

    entries = {role: {"connection_id": "c"} for role in ("chief", "strategist", "critic")}
    assert not await smoke_test(office(entries, {"c": Says("m")}))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["agent"] for r in records] == ["chief", "researcher", "strategist", "critic"]
    assert [r["status"] for r in records] == ["ok", "failed", "ok", "ok"]
    assert records[1]["error"] == "role_has_no_model"
