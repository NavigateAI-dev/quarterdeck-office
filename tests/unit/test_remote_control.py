import importlib.util
import json
from pathlib import Path
from typing import Any

spec = importlib.util.spec_from_file_location("control", Path("remote/control.py"))
assert spec and spec.loader
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)

ROUTED = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {
        "quarterdeck.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}
    },
}
RUNNING = {
    "BackendState": "Running",
    "Self": {"DNSName": "quarterdeck.tail1234.ts.net.", "Online": True},
}


def fake(answers: dict[str, tuple[int, str]], said: list[list[str]]) -> Any:
    def runner(command: list[str], timeout: float) -> tuple[int, str]:
        said.append(command)
        return answers.get(" ".join(command[1:]), (0, ""))

    return runner


def test_serve_counts_only_when_it_routes_to_the_office() -> None:
    assert control.routed(ROUTED, "quarterdeck.tail1234.ts.net")
    elsewhere = json.loads(json.dumps(ROUTED).replace("127.0.0.1:8000", "127.0.0.1:9000"))
    assert not control.routed(elsewhere, "quarterdeck.tail1234.ts.net")
    assert not control.routed(
        {**ROUTED, "AllowFunnel": {"x:443": True}}, "quarterdeck.tail1234.ts.net"
    )


def test_the_route_is_turned_on_with_serve_and_never_funnel() -> None:
    said: list[list[str]] = []
    runner = fake(
        {
            "status --json": (0, json.dumps(RUNNING)),
            "serve status --json": (0, json.dumps(ROUTED)),
        },
        said,
    )
    found = control.route_on(runner)
    assert found["ok"] and found["dns_name"] == "quarterdeck.tail1234.ts.net"
    assert [control.TAILSCALE, "serve", "--bg", "--https=443", "http://127.0.0.1:8000"] in said
    assert not any("funnel" in part for command in said for part in command)


def test_https_not_enabled_in_the_tailnet_is_reported_with_its_link() -> None:
    said: list[list[str]] = []
    runner = fake(
        {
            "serve --bg --https=443 http://127.0.0.1:8000": (
                124,
                "HTTPS is not enabled. Enable it at https://login.tailscale.com/f/serve?node=n1",
            ),
            "status --json": (0, json.dumps(RUNNING)),
            "serve status --json": (0, "{}"),
        },
        said,
    )
    found = control.route_on(runner)
    assert not found["ok"] and found["needs_https"]
    assert found["url"] == "https://login.tailscale.com/f/serve?node=n1"


def test_an_auth_key_is_read_from_a_file_and_never_echoed(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(control, "KEY_FILE", tmp_path / "key")
    said: list[list[str]] = []
    key = "tskey-auth-SECRET"

    def runner(command: list[str], timeout: float) -> tuple[int, str]:
        said.append(command)
        return 0, f"up with {key}"

    found = control.connect({"auth_key": key}, runner)
    assert key not in json.dumps(said) and key not in json.dumps(found)
    assert any(part.startswith("--auth-key=file:") for part in said[0])
    assert not (tmp_path / "key").exists()


def test_a_request_file_is_removed_before_it_runs(tmp_path: Path, monkeypatch: Any) -> None:
    job = tmp_path / "a.job.json"
    job.write_text(json.dumps({"action": "nothing-known", "auth_key": "k"}))
    control.answer(job)
    assert not job.exists()
    assert json.loads((tmp_path / "a.result.json").read_text())["ok"] is False
