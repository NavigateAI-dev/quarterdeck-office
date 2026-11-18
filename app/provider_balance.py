"""What is left on a provider account, where the provider publishes it.

Read with the connection's own key, from the provider's own endpoint:

- DeepSeek: `GET /user/balance` — whether the key can be used, and the balance.
- OpenRouter: `GET /api/v1/key` — the key's spend and, when it has one, its limit.

Anthropic and OpenAI publish usage and cost only to a separate admin key, which
the office does not hold; the office says so rather than guessing. A balance is
read at most every few minutes, and a failure is a state to show, not an error.
Warnings follow the office's other limits: once per crossing. A balance that
recovers clears its warning, so running low again is a new crossing.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.credentials import resolve_credential
from app.db.models import LimitAlert, ModelConnection
from app.mcp.client import McpError, check_address
from app.secret_store import SecretStoreError

TIMEOUT_SECONDS = 10.0
CACHE_SECONDS = 300
# Below this much left, a balance is running low, in the account's own
# currency (11 Oct: DeepSeek bills a Chinese account in yuan, about 7 to the
# dollar, so 2 meant two yuan in one place and two dollars in another).
LOW_BALANCE = 2.0
LOW_BY_CURRENCY = {"USD": 2.0, "CNY": 15.0}
ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/user/balance",
    "openrouter": "https://openrouter.ai/api/v1/key",
    # Moonshot (Kimi) and SiliconFlow say what is left on the key's account.
    "moonshot": "https://api.moonshot.ai/v1/users/me/balance",
    "moonshot_cn": "https://api.moonshot.cn/v1/users/me/balance",
    "siliconflow": "https://api.siliconflow.com/v1/user/info",
    "siliconflow_cn": "https://api.siliconflow.cn/v1/user/info",
}
# The account's currency, which neither reply names: each region bills in its own.
CURRENCY = {"moonshot": "USD", "moonshot_cn": "CNY", "siliconflow": "USD", "siliconflow_cn": "CNY"}
NO_BALANCE = {
    "anthropic": (
        "Anthropic publishes usage and cost only to an admin key, which the office does not hold."
    ),
    "openai": (
        "OpenAI publishes usage and cost only to an admin key, which the office does not hold."
    ),
}
# connection id -> (read at, what was read)
CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


async def read_balance(
    provider: str,
    base_url: str | None,
    key: str,
    allow_list: list[str],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """What one provider says is left on the account behind this key."""
    endpoint = ENDPOINTS.get(provider)
    if endpoint is None:
        reason = NO_BALANCE.get(
            provider, "This provider does not publish a balance the office can read."
        )
        return {
            "state": "unsupported",
            "reason": reason,
            "usable": None,
            "balance": None,
            "limit": None,
        }
    http = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)
    try:
        try:
            checked = check_address(endpoint, allow_list)
        except McpError as refused:
            return _unavailable(refused.code)
        try:
            response = await http.get(
                checked, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}
            )
        except httpx.HTTPError:
            return _unavailable("provider_unreachable")
        if response.status_code in (401, 403):
            return _unavailable("key_refused")
        if response.status_code != 200:
            return _unavailable("provider_unavailable")
        try:
            body = response.json()
        except ValueError:
            return _unavailable("provider_unreadable")
    finally:
        if client is None:
            await http.aclose()
    if provider in ("moonshot", "moonshot_cn", "siliconflow", "siliconflow_cn"):
        return _account_balance(provider, body)
    if provider == "deepseek":
        infos = [info for info in body.get("balance_infos") or [] if isinstance(info, dict)]
        # One entry per currency, and the first is not always the one with the
        # money (11 Oct: USD 0.00 listed before CNY 45.14). The account is the
        # currency that holds the most, in its own terms.
        held = max(infos, key=lambda info: _number(info.get("total_balance")), default={})
        return {
            "state": "ok",
            "reason": "",
            "usable": bool(body.get("is_available")),
            "balance": {
                "currency": str(held.get("currency") or ""),
                "total": _number(held.get("total_balance")),
                "granted": _number(held.get("granted_balance")),
                "topped_up": _number(held.get("topped_up_balance")),
                "all": [
                    {
                        "currency": str(info.get("currency") or ""),
                        "total": _number(info.get("total_balance")),
                    }
                    for info in infos
                ],
            }
            if held
            else None,
            "limit": None,
        }
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    limit = _number(data.get("limit")) if data.get("limit") is not None else None
    usage = _number(data.get("usage"))
    remaining = (
        _number(data.get("limit_remaining")) if data.get("limit_remaining") is not None else None
    )
    return {
        "state": "ok",
        "reason": "",
        "usable": None if remaining is None else remaining > 0,
        "balance": None,
        "limit": {
            "limit": limit,
            "usage": usage,
            "remaining": remaining,
            "share": round(usage / limit, 4) if limit else None,
        },
    }


def _account_balance(provider: str, body: Any) -> dict[str, Any]:
    """Moonshot's or SiliconFlow's reply: what is left, given and paid for."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return _unavailable("provider_unreadable")
    if provider.startswith("moonshot"):
        fields = ("available_balance", "voucher_balance", "cash_balance")
    else:
        fields = ("totalBalance", "balance", "chargeBalance")
    if fields[0] not in data:
        return _unavailable("provider_unreadable")
    total, granted, paid = (_number(data.get(name)) for name in fields)
    return {
        "state": "ok",
        "reason": "",
        "usable": total > 0,
        "balance": {
            "currency": CURRENCY[provider],
            "total": total,
            "granted": granted,
            "topped_up": paid,
        },
        "limit": None,
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "state": "unavailable",
        "reason": reason,
        "usable": None,
        "balance": None,
        "limit": None,
    }


def _number(value: Any) -> float:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0


async def balances(
    session_factory: Any, settings: Settings, allow_list: list[str]
) -> list[dict[str, Any]]:
    """Every connection's balance, read at most every few minutes, with warnings."""
    with session_factory() as session:
        connections = [
            (str(row.id), row.display_name, row.provider, row.base_url, row.credential_ref)
            for row in session.scalars(
                select(ModelConnection).order_by(ModelConnection.display_name)
            )
        ]
    now = time.monotonic()
    found = []
    for connection_id, name, provider, base_url, reference in connections:
        cached = CACHE.get(connection_id)
        if cached and now - cached[0] < CACHE_SECONDS:
            reading = cached[1]
        else:
            if provider not in ENDPOINTS:
                reading = await read_balance(provider, base_url, "", allow_list)
            elif not reference:
                reading = _unavailable("no_key")
            else:
                try:
                    key = resolve_credential(reference, settings)
                except SecretStoreError:
                    reading = _unavailable("no_key")
                else:
                    reading = await read_balance(provider, base_url, key, allow_list)
            reading = {**reading, "checked_at": datetime.now(UTC).isoformat()}
            CACHE[connection_id] = (now, reading)
        found.append(
            {"connection_id": connection_id, "name": name, "provider": provider, **reading}
        )
    with session_factory() as session, session.begin():
        for entry in found:
            warn(session, entry)
    return found


def standing(entry: dict[str, Any]) -> tuple[bool, bool, float | None]:
    """(running low, used up, share used) for one reading, in its own currency."""
    share: float | None = None
    low = False
    empty = entry.get("usable") is False
    if entry.get("balance"):
        balance = entry["balance"]
        total = float(balance.get("total") or 0)
        floor = LOW_BY_CURRENCY.get(str(balance.get("currency") or "").upper(), LOW_BALANCE)
        low = total < floor
        empty = empty or total <= 0
        share = 1.0 if empty else (0.8 if low else None)
    elif entry.get("limit") and entry["limit"].get("share") is not None:
        share = float(entry["limit"]["share"])
        low = share >= 0.8
        empty = empty or share >= 1.0
    return low or empty, empty, share


def warn(session: Session, entry: dict[str, Any]) -> None:
    """Running low (80) or used up (100), once per crossing; cleared on recovery."""
    if entry.get("state") != "ok":
        return
    subject = entry["connection_id"]
    low, empty, share = standing(entry)
    crossed = {80: low, 100: empty}
    for threshold, now_crossed in crossed.items():
        if now_crossed:
            session.execute(
                insert(LimitAlert)
                .values(
                    scope="provider",
                    subject=subject,
                    name=str(entry.get("name") or "")[:255],
                    window="balance",
                    threshold=threshold,
                    share=share if share is not None else 1.0,
                )
                .on_conflict_do_nothing(index_elements=["scope", "subject", "window", "threshold"])
            )
        else:
            session.execute(
                delete(LimitAlert).where(
                    LimitAlert.scope == "provider",
                    LimitAlert.subject == subject,
                    LimitAlert.window == "balance",
                    LimitAlert.threshold == threshold,
                )
            )
