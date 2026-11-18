"""Run notifications as the first plugin: the engine announces, the plugin listens."""

from typing import Any
from uuid import uuid4

import pytest

from app.config import get_settings
from app.kernel import EventBus, Scope, load_plugins
from app.plugins import BUILTIN_PLUGINS
from app.plugins.notifications import NotificationsPlugin
from app.runtime.events import RunSettled
from app.runtime.worker import RunWorker


def office_with(events: EventBus) -> Scope:
    root = Scope("root")
    root.provide("events", events)
    root.provide("settings", get_settings())
    root.provide("session_factory", object())
    return root.child("office", "main")


async def test_a_settled_run_is_queued_for_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[tuple[Any, str]] = []
    monkeypatch.setattr(
        "app.plugins.notifications.notify_run",
        lambda factory, run_id, status, *args: queued.append((run_id, status)),
    )
    events = EventBus()
    office = office_with(events)
    load_plugins(office, [NotificationsPlugin()])
    run_id = uuid4()

    await events.publish(RunSettled(run_id, "COMPLETED"))

    assert queued == [(run_id, "COMPLETED")]


async def test_disposing_the_office_stops_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[Any] = []
    monkeypatch.setattr("app.plugins.notifications.notify_run", lambda *args: queued.append(args))
    events = EventBus()
    office = office_with(events)
    load_plugins(office, [NotificationsPlugin()])

    office.dispose()
    await events.publish(RunSettled(uuid4(), "FAILED"))

    assert queued == []


async def test_the_worker_announces_instead_of_calling_channels() -> None:
    seen: list[RunSettled] = []
    events = EventBus()
    events.subscribe(RunSettled, seen.append)
    worker = RunWorker(get_settings(), repository=None, events=events)  # type: ignore[arg-type]
    run_id = uuid4()

    await worker._notify(run_id, "WAITING_FOR_APPROVAL")

    assert seen == [RunSettled(run_id, "WAITING_FOR_APPROVAL")]


def test_notifications_is_a_builtin_plugin() -> None:
    assert [plugin.name for plugin in BUILTIN_PLUGINS] == ["notifications"]
