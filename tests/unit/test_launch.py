import socket
from pathlib import Path

from scripts.launch import (
    Check,
    check_disk,
    check_port,
    env_value,
    sign_in_url,
)


def test_a_free_port_passes() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert check_port(port, owned_by_office=False).ok


def test_a_port_held_by_something_else_fails_with_a_fix() -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        result = check_port(port, owned_by_office=False)
    assert not result.ok
    assert str(port) in result.fix


def test_a_port_held_by_the_running_office_is_fine() -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        assert check_port(holder.getsockname()[1], owned_by_office=True).ok


def test_low_disk_fails(tmp_path: Path) -> None:
    assert check_disk(tmp_path, minimum_bytes=1).ok
    assert not check_disk(tmp_path, minimum_bytes=10**18).ok


def test_env_values_are_read_without_printing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nOWNER_API_TOKEN=abc=def\nOTHER=1\n")
    assert env_value(env, "OWNER_API_TOKEN") == "abc=def"
    assert env_value(env, "MISSING") is None


def test_the_sign_in_code_travels_in_the_fragment() -> None:
    url = sign_in_url("http://127.0.0.1:8000", "CODE123")
    assert url == "http://127.0.0.1:8000/#code=CODE123"
    assert "?" not in url


def test_a_check_carries_its_fix() -> None:
    assert Check("docker", False, "Start Docker Desktop").fix == "Start Docker Desktop"
