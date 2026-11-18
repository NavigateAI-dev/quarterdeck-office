from pathlib import Path

from scripts import access


def answers(table: dict[str, str]) -> access.Runner:
    return lambda command: table.get(" ".join(command), "")


def test_the_mac_addresses_are_its_own_private_ones_then_its_name() -> None:
    run = answers(
        {
            "ipconfig getifaddr en0": "192.168.1.50",
            "ipconfig getifaddr en1": "",
            "scutil --get LocalHostName": "Mac-mini",
        }
    )
    assert access.lan_addresses(run) == [
        "http://192.168.1.50:8000",
        "http://mac-mini.local:8000",
    ]


def test_a_public_or_missing_address_is_not_offered() -> None:
    run = answers({"ipconfig getifaddr en0": "8.8.8.8", "scutil --get LocalHostName": "Mac-mini"})
    assert access.lan_addresses(run) == []


def test_settings_are_replaced_in_place_or_added() -> None:
    text = "OWNER_NAME=Alex\nOFFICE_BIND=127.0.0.1\n"
    changed = access.with_values(text, {"OFFICE_BIND": "0.0.0.0", "OFFICE_LAN_ADDRESSES": "x"})  # noqa: S104
    assert changed == "OWNER_NAME=Alex\nOFFICE_BIND=0.0.0.0\nOFFICE_LAN_ADDRESSES=x\n"


def test_no_network_changes_nothing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OFFICE_BIND=127.0.0.1\n")
    assert access.main(["home"], run=answers({}), env_file=env) == 1
    assert env.read_text() == "OFFICE_BIND=127.0.0.1\n"
