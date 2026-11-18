"""Where the office's browser may go, and which presses need the owner.

The browser reaches the public web only: an address that resolves to a
private, loopback, link-local or reserved one is refused, so a page cannot
steer it at the office itself, the owner's router or anything on their
network. Pressing what signs in, submits, sends, pays, books or deletes, and
typing a password, is the owner's to allow.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCHEMES = ("http", "https")
# Words on a button or link that mean the press does something in the world.
CONSEQUENTIAL = re.compile(
    r"\b(sign ?in|log ?in|log ?on|sign ?up|register|create (an )?account|submit|send|pay|"
    r"buy|order|checkout|check out|purchase|confirm|delete|remove|subscribe|unsubscribe|"
    r"book|reserve|transfer|donate|post|publish|apply|upload|accept|agree)\b"
    r"|登录|登入|注册|提交|发送|支付|付款|购买|下单|确认|删除|订阅|预订|预约|转账|发布|上传|同意",
    re.IGNORECASE,
)


# A search box: pressing Enter in it looks something up and changes nothing.
SEARCH = re.compile(
    r"search|find|look ?up|zoek|suche|recherche|buscar|搜索|查找|検索", re.IGNORECASE
)


def searching(element: dict[str, Any]) -> bool:
    kind = str(element.get("type") or "").lower()
    role = str(element.get("role") or "").lower()
    return (
        kind == "search"
        or role == "searchbox"
        or bool(SEARCH.search(str(element.get("label") or "")))
    )


def address_public(host: str) -> bool:
    """Whether every address the host resolves to is on the public internet."""
    name = host.strip("[]").lower()
    if not name or name in ("localhost",) or name.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        literal = None
    if literal is not None:
        return literal.is_global
    try:
        found = socket.getaddrinfo(name, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    addresses = {item[4][0] for item in found}
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


def url_allowed(url: str) -> bool:
    """A web address on the public internet; never a file, a browser page or the office."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in SCHEMES and bool(parts.hostname) and address_public(parts.hostname or "")


def needs_owner(action: str, element: dict[str, Any] | None, submit: bool = False) -> str | None:
    """Why this action is the owner's to allow, or None when the agent may go on."""
    if element is None:
        return None
    label = str(element.get("label") or "")
    kind = str(element.get("type") or "").lower()
    if action == "type" and kind == "password":
        return "type a password"
    if action == "type" and submit and not searching(element):
        return f"submit the form at “{label[:60] or 'a field'}”"
    if action == "click" and (kind == "submit" or CONSEQUENTIAL.search(label)):
        return f"press “{label[:60] or 'a submit button'}”"
    return None


# Parts of an address that carry a sign-in or a secret, never kept (8 Oct: a
# login page's address carried its token into the task's record).
SECRET_PART = re.compile(r"token|auth|session|sid|key|code|pass|secret|sig|nonce|ticket", re.I)


def clean_url(url: str) -> str:
    """The address as kept in the task's record: no secret-bearing parts."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not SECRET_PART.search(k)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def web_address(text: str) -> str | None:
    """A public web address, or None: no local files, no other schemes."""
    address = text.strip()
    if not re.match(r"^https?://[^\s/]+", address):
        return None
    return address if url_allowed(address) else None
