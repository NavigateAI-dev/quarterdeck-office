"""I1 stage 4: what is left on a provider account, where the provider says."""

import httpx
import pytest

from app.provider_balance import read_balance

KEY = "sk-balance-key-0123456789"


@pytest.fixture(autouse=True)
def resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def resolved(host: str, port: int, *args: object, **kwargs: object) -> list:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolved)


def provider(status: int, body: dict, seen: list) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_deepseek_says_its_balance() -> None:
    seen: list = []
    body = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": "12.30",
                "granted_balance": "0.00",
                "topped_up_balance": "12.30",
            }
        ],
    }
    found = await read_balance("deepseek", None, KEY, [], client=provider(200, body, seen))
    assert found["state"] == "ok" and found["usable"] is True
    assert found["balance"] == {
        "currency": "CNY",
        "total": 12.3,
        "granted": 0.0,
        "topped_up": 12.3,
        "all": [{"currency": "CNY", "total": 12.3}],
    }
    assert str(seen[0].url) == "https://api.deepseek.com/user/balance"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in str(found)


async def test_openrouter_says_its_key_limit_and_use() -> None:
    seen: list = []
    body = {"data": {"label": "office", "usage": 8.5, "limit": 10.0, "limit_remaining": 1.5}}
    found = await read_balance("openrouter", None, KEY, [], client=provider(200, body, seen))
    assert found["state"] == "ok"
    assert found["limit"] == {"limit": 10.0, "usage": 8.5, "remaining": 1.5, "share": 0.85}
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/key"


async def test_openrouter_without_a_limit_says_only_its_use() -> None:
    body = {"data": {"usage": 3.2, "limit": None}}
    found = await read_balance("openrouter", None, KEY, [], client=provider(200, body, []))
    assert found["limit"] == {"limit": None, "usage": 3.2, "remaining": None, "share": None}


@pytest.mark.parametrize("name", ["anthropic", "openai", "groq"])
async def test_a_provider_that_publishes_no_balance_says_why(name: str) -> None:
    found = await read_balance(name, None, KEY, [])
    assert found["state"] == "unsupported" and found["reason"]


async def test_a_refused_key_is_a_state_not_an_error() -> None:
    found = await read_balance("deepseek", None, KEY, [], client=provider(401, {}, []))
    assert found["state"] == "unavailable" and found["reason"] == "key_refused"
    assert KEY not in str(found)


async def test_moonshot_says_what_is_left_in_its_regions_currency() -> None:
    """7 Oct: Kimi's account balance, international in USD, China in CNY."""
    seen: list = []
    body = {
        "code": 0,
        "data": {
            "available_balance": 49.58894,
            "voucher_balance": 46.58893,
            "cash_balance": 3.00001,
        },
        "status": True,
    }
    async with provider(200, body, seen) as client:
        read = await read_balance("moonshot_cn", None, KEY, [], client)
    assert str(seen[0].url) == "https://api.moonshot.cn/v1/users/me/balance"
    assert read["state"] == "ok" and read["usable"] is True
    assert read["balance"] == {
        "currency": "CNY",
        "total": 49.5889,
        "granted": 46.5889,
        "topped_up": 3.0,
    }


async def test_siliconflow_says_its_balance_and_an_empty_one_is_not_usable() -> None:
    seen: list = []
    body = {"code": 20000, "data": {"balance": "0", "chargeBalance": "0", "totalBalance": "0"}}
    async with provider(200, body, seen) as client:
        read = await read_balance("siliconflow", None, KEY, [], client)
    assert str(seen[0].url) == "https://api.siliconflow.com/v1/user/info"
    assert read["usable"] is False and read["balance"]["currency"] == "USD"


async def test_a_balance_reply_in_an_unknown_shape_is_not_guessed() -> None:
    async with provider(200, {"data": {"something": 1}}, []) as client:
        read = await read_balance("siliconflow_cn", None, KEY, [], client)
    assert read["state"] == "unavailable" and read["reason"] == "provider_unreadable"


async def test_deepseek_is_judged_by_the_currency_that_holds_the_money() -> None:
    """11 Oct: DeepSeek lists USD 0.00 before the CNY the owner paid in; the
    office read the first and said the account had run out."""
    body = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "USD",
                "total_balance": "0.00",
                "granted_balance": "0.00",
                "topped_up_balance": "0.00",
            },
            {
                "currency": "CNY",
                "total_balance": "45.14",
                "granted_balance": "0.00",
                "topped_up_balance": "45.14",
            },
        ],
    }
    found = await read_balance("deepseek", None, KEY, [], client=provider(200, body, []))
    assert found["balance"]["currency"] == "CNY" and found["balance"]["total"] == 45.14
    assert found["balance"]["all"] == [
        {"currency": "USD", "total": 0.0},
        {"currency": "CNY", "total": 45.14},
    ]


@pytest.mark.parametrize(
    ("balance", "usable", "low", "empty"),
    [
        ({"currency": "CNY", "total": 45.14}, True, False, False),
        ({"currency": "CNY", "total": 10.0}, True, True, False),
        ({"currency": "USD", "total": 10.0}, True, False, False),
        ({"currency": "USD", "total": 1.5}, True, True, False),
        ({"currency": "CNY", "total": 0.0}, True, True, True),
        ({"currency": "CNY", "total": 45.14}, False, False, True),
    ],
)
def test_low_is_judged_in_the_accounts_own_currency(
    balance: dict, usable: bool, low: bool, empty: bool
) -> None:
    from app.provider_balance import standing

    entry = {"state": "ok", "usable": usable, "balance": balance}
    assert standing(entry)[:2] == (low or empty, empty)
