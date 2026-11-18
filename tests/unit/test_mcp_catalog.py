"""The reviewed catalog of tool servers shipped with the office."""

from urllib.parse import urlparse

from app.mcp.catalog import CATEGORIES, load


def test_the_catalog_loads_and_every_entry_is_complete() -> None:
    catalog = load()
    assert catalog.entries, "an empty catalog offers nothing"
    for entry in catalog.entries:
        assert entry.what.strip() and entry.vendor.strip()
        assert entry.category in CATEGORIES


def test_every_address_is_https_on_a_public_host() -> None:
    for entry in load().entries:
        parsed = urlparse(entry.url)
        assert parsed.scheme == "https", entry.id
        assert parsed.hostname and "." in parsed.hostname, entry.id


def test_ids_and_addresses_are_unique() -> None:
    entries = load().entries
    assert len({e.id for e in entries}) == len(entries)
    assert len({e.url.rstrip("/") for e in entries}) == len(entries)


def test_a_key_in_the_address_is_never_catalogued() -> None:
    """The address is shown and stored openly; a key belongs in the key field."""
    for entry in load().entries:
        assert "key" not in (urlparse(entry.url).query or "").lower(), entry.id


def test_a_server_needing_a_key_says_where_to_get_one() -> None:
    for entry in load().entries:
        if entry.sign_in == "key":
            assert entry.key_help.strip(), entry.id


def test_account_sign_in_servers_can_be_added_when_the_office_can_register() -> None:
    """N11: an account server is added, then signed in to from its card, unless
    its vendor issues sign-in clients only by hand."""
    for entry in load().entries:
        assert entry.addable is (entry.sign_in != "account" or entry.self_registers)
    held_back = {entry.id for entry in load().entries if not entry.addable}
    # Checked on 2026-10-01: Box and Slack issue sign-in clients only by hand too.
    assert held_back == {"asana", "box", "hubspot", "slack"}


def test_a_relay_says_so() -> None:
    """A relay of someone else's service must be marked, never passed off as the vendor."""
    for entry in load().entries:
        host = urlparse(entry.url).hostname or ""
        if entry.relay:
            assert entry.note.strip(), entry.id
        else:
            # A first-party entry lives on the vendor's own domain.
            assert entry.vendor_domain in host, (entry.id, host)
