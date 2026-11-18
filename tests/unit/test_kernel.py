"""The office kernel: scopes, registries, hooks, events and the plugin loader."""

from dataclasses import dataclass
from typing import Any

import pytest

from app.kernel import (
    Denied,
    EventBus,
    HookBus,
    PluginError,
    Registry,
    Scope,
    load_plugins,
)

# ---- scope ----


def test_dispose_runs_children_first_then_own_cleanups_in_reverse() -> None:
    order: list[str] = []
    root = Scope("root")
    office = root.child("office", "main")
    run = office.child("run", "r1")
    office.defer(lambda: order.append("office-1"))
    office.defer(lambda: order.append("office-2"))
    run.defer(lambda: order.append("run"))

    office.dispose()

    assert order == ["run", "office-2", "office-1"]
    assert office.disposed and run.disposed and not root.disposed


def test_dispose_is_idempotent_and_detaches_from_parent() -> None:
    root = Scope("root")
    child = root.child("office", "main")
    child.dispose()
    child.dispose()
    assert root.children == []


def test_a_failing_cleanup_does_not_stop_the_others(caplog: pytest.LogCaptureFixture) -> None:
    order: list[str] = []
    scope = Scope("root")
    scope.defer(lambda: order.append("first"))
    scope.defer(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    scope.defer(lambda: order.append("last"))

    scope.dispose()

    assert order == ["last", "first"]
    assert "scope_cleanup_failed" in caplog.text


def test_services_resolve_up_the_tree_and_are_removed_with_their_scope() -> None:
    root = Scope("root")
    root.provide("db", "root-db")
    office = root.child("office", "main")
    office.provide("calendar", "cal")
    run = office.child("run", "r1")

    assert run.get("db") == "root-db"
    assert run.get("calendar") == "cal"
    office.dispose()
    with pytest.raises(LookupError, match="calendar"):
        root.get("calendar")


def test_export_offers_a_service_to_the_parent_until_the_child_ends() -> None:
    office = Scope("root").child("office", "main")
    plugin = office.child("plugin", "calendar")

    plugin.export("calendar", "cal")
    assert office.get("calendar") == "cal"

    plugin.dispose()
    assert not office.has("calendar")


def test_a_service_name_is_provided_once_per_scope() -> None:
    root = Scope("root")
    root.provide("db", 1)
    with pytest.raises(ValueError, match="db"):
        root.provide("db", 2)


def test_a_disposed_scope_refuses_new_work() -> None:
    scope = Scope("root")
    scope.dispose()
    with pytest.raises(RuntimeError):
        scope.defer(lambda: None)
    with pytest.raises(RuntimeError):
        scope.child("office", "x")


# ---- registry ----


def test_registry_register_returns_a_disposer() -> None:
    roles: Registry[str] = Registry("roles")
    remove = roles.register("critic", "Lee")
    assert roles.get("critic") == "Lee"
    assert roles.names() == ["critic"]
    remove()
    assert roles.get("critic") is None
    remove()  # removing twice is harmless


def test_registry_refuses_a_duplicate_name() -> None:
    roles: Registry[str] = Registry("roles")
    roles.register("critic", "Lee")
    with pytest.raises(ValueError, match="critic"):
        roles.register("critic", "Other")


def test_scope_register_is_undone_when_the_scope_is_disposed() -> None:
    roles: Registry[str] = Registry("roles")
    plugin = Scope("root").child("plugin", "crm")
    plugin.register(roles, "account-manager", "Ann")
    plugin.dispose()
    assert roles.get("account-manager") is None


# ---- hooks ----


async def test_waterfall_handlers_run_in_order_and_each_sees_the_last_value() -> None:
    hooks = HookBus()
    hooks.on("step.before", lambda text: text + " b", order=20)
    hooks.on("step.before", lambda text: text + " a", order=10)

    assert await hooks.waterfall("step.before", "x") == "x a b"


async def test_waterfall_accepts_async_handlers() -> None:
    hooks = HookBus()

    async def shout(text: str) -> str:
        return text.upper()

    hooks.on("step.before", shout)
    assert await hooks.waterfall("step.before", "quiet") == "QUIET"


async def test_guards_can_only_deny_and_the_first_denial_wins() -> None:
    hooks = HookBus()
    hooks.guard("tool.call", lambda subject: None)
    hooks.guard("tool.call", lambda subject: "no payments" if subject == "pay" else None)
    hooks.guard("tool.call", lambda subject: "a later reason" if subject == "pay" else None)

    await hooks.check("tool.call", "search")  # allowed: every guard returned None
    with pytest.raises(Denied, match="no payments"):
        await hooks.check("tool.call", "pay")


async def test_a_guard_that_fails_denies() -> None:
    hooks = HookBus()

    def broken(subject: Any) -> str | None:
        raise RuntimeError("guard crashed")

    hooks.guard("tool.call", broken)
    with pytest.raises(Denied, match="guard failed"):
        await hooks.check("tool.call", "search")


async def test_hook_disposers_remove_handlers() -> None:
    hooks = HookBus()
    remove = hooks.on("step.before", lambda text: text + "!")
    remove()
    assert await hooks.waterfall("step.before", "x") == "x"


# ---- events ----


@dataclass(frozen=True)
class RunEnded:
    run_id: str
    status: str


async def test_events_reach_subscribers_of_their_type() -> None:
    events = EventBus()
    seen: list[RunEnded] = []
    events.subscribe(RunEnded, seen.append)

    await events.publish(RunEnded("r1", "COMPLETED"))

    assert seen == [RunEnded("r1", "COMPLETED")]


async def test_a_failing_subscriber_never_reaches_the_publisher(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events = EventBus()
    seen: list[RunEnded] = []

    async def broken(event: RunEnded) -> None:
        raise RuntimeError("slack down")

    events.subscribe(RunEnded, broken)
    events.subscribe(RunEnded, seen.append)

    await events.publish(RunEnded("r1", "FAILED"))

    assert len(seen) == 1
    assert "event_subscriber_failed" in caplog.text


# ---- plugins ----


@dataclass
class FakePlugin:
    name: str
    requires: tuple[str, ...] = ()
    calls: list[str] | None = None
    fail: bool = False

    def setup(self, scope: Scope) -> None:
        if self.calls is not None:
            self.calls.append(self.name)
        scope.export(f"{self.name}.ready", True)
        if self.fail:
            raise RuntimeError("cannot start")


def test_plugins_load_in_order_each_in_its_own_scope() -> None:
    calls: list[str] = []
    office = Scope("root").child("office", "main")

    loaded = load_plugins(
        office, [FakePlugin("slack", calls=calls), FakePlugin("calendar", calls=calls)]
    )

    assert calls == ["slack", "calendar"]
    assert [scope.id for scope in loaded.values()] == ["slack", "calendar"]
    assert office.get("slack.ready") is True


def test_a_missing_requirement_fails_loudly_before_anything_starts() -> None:
    calls: list[str] = []
    office = Scope("root").child("office", "main")

    with pytest.raises(PluginError, match="crm.*needs.*hubspot"):
        load_plugins(
            office, [FakePlugin("slack", calls=calls), FakePlugin("crm", requires=("hubspot",))]
        )
    assert calls == []


def test_a_plugin_that_fails_to_start_is_cleaned_up_and_named() -> None:
    office = Scope("root").child("office", "main")

    with pytest.raises(PluginError, match="broken.*cannot start"):
        load_plugins(office, [FakePlugin("broken", fail=True)])
    with pytest.raises(LookupError):
        office.get("broken.ready")


def test_duplicate_plugin_names_are_refused() -> None:
    office = Scope("root").child("office", "main")
    with pytest.raises(PluginError, match="twice"):
        load_plugins(office, [FakePlugin("slack"), FakePlugin("slack")])
