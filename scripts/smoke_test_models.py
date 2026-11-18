"""Ask each role's model to reply OK, through the connection chosen for it.

    docker compose exec api python -m scripts.smoke_test_models

One JSON line per role; exits non-zero when any role fails or has no model.
"""

import asyncio
import json
import sys

from pydantic import ValidationError

from app.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.gateway.client import GatewayError
from app.office_models import OfficeModels, office_models


async def smoke_test(models: OfficeModels) -> bool:
    passed = True
    for role, alias in models.settings.model_aliases.items():
        record: dict[str, object] = {"agent": role, "model_alias": alias}
        try:
            result = await models.complete(
                alias, [{"role": "user", "content": "Reply with exactly OK."}]
            )
        except GatewayError as exc:
            passed = False
            record.update(status="failed", error=exc.code, http_status=exc.status_code)
        else:
            accepted = result.content.strip() == "OK"
            passed = passed and accepted
            record.update(
                status="ok" if accepted else "failed",
                model=result.model,
                latency_seconds=round(result.latency_seconds, 3),
                input_tokens=result.usage.prompt_tokens,
                output_tokens=result.usage.completion_tokens,
                estimated_cost=result.estimated_cost,
            )
            if not accepted:
                record["error"] = "unexpected_smoke_response"
        print(json.dumps(record), flush=True)
    return passed


async def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        print(json.dumps({"status": "configuration_error", "fields": fields}), file=sys.stderr)
        return 2
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        models = office_models(settings, make_session_factory(engine))
        return 0 if await smoke_test(models) else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
