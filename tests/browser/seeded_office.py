"""A throwaway office for browser tests, which change data.

    python tests/browser/seeded_office.py <port> [<backup archive>]

Browser tests click, create and archive things, so they never run against an
office in use: this starts one in its own PostgreSQL schema
(TEST_DATABASE_URL), prints one JSON line saying where its tasks are, and
serves until stopped. The schema is dropped on the way out.

With no archive it holds two finished tasks, a brief and a deck. With one it
is a copy of the office the archive came from, so the tests read real work.
Either way it cannot spend: its model gateway is an address nothing answers,
and the copy's stored keys were sealed with the original's root key, which a
throwaway office never has.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import text, update

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.artifacts import ArtifactStore  # noqa: E402
from app.backup import restore_backup  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import (  # noqa: E402
    ChannelApp,
    ChannelDestination,
    ChannelInstallation,
    ModelConnection,
    Project,
    RoomMessage,
    Run,
    Schedule,
    TaskTemplate,
    Topic,
)
from app.db.session import make_engine, make_session_factory  # noqa: E402
from app.gateway.client import Completion, Usage  # noqa: E402
from app.main import create_app  # noqa: E402
from app.model_calls import ModelCalls, request_of  # noqa: E402
from app.orchestrator.checkpoints import open_checkpointer  # noqa: E402
from app.orchestrator.runner import RunRepository  # noqa: E402
from app.secret_store import generate_root_key  # noqa: E402

TOKEN = "owner-seeded-office-token-of-sufficient-length"
# A routine's instructions, long enough that repeating them would bury the recaps.
RECAP_ASKS = "My morning recap, in this order:\n" + "\n".join(
    f"- Section {n}: what changed and why it matters to me" for n in range(1, 13)
)
RECAP_REPLY = "\n".join(f"- Section {n}: nothing new worth your time." for n in range(1, 25))

BRIEF = {
    "recommendation": "Our onboarding covers identification well. It falls short in three places.",
    "facts": ["The handbook has no step for ongoing monitoring."],
    "deliverable_body": [
        {
            "heading": "Where we fall short",
            "points": ["Ongoing monitoring stops at acceptance.", "Layered owners go unchecked."],
        },
        {"heading": "What would change it", "points": ["Monitoring done outside the handbook."]},
    ],
}
# A dashboard: its numbers in a table the office draws.
DASHBOARD = {
    "lead": "Sign-ups tripled this year.",
    "facts": ["Counted from the admin export."],
    "recommendation": "Keep the campaign running.",
    "deliverable_body": [
        {
            "heading": "Sign-ups by quarter",
            "points": ["Up every quarter."],
            "table": {
                "columns": ["Quarter", "Sign-ups"],
                "rows": [["Q1 2025", "100"], ["Q2 2025", "180"], ["Q3 2025", "300"]],
            },
        }
    ],
}
DECK = {
    "recommendation": "Close the monitoring gap before year end.",
    "deliverable_body": [
        {"heading": "What changed this quarter", "points": ["Two new clients", "A review done"]},
        {"heading": "Where we fall short", "points": ["Ongoing monitoring"]},
        {"heading": "The decision we need", "points": ["Approve a monitoring step"]},
    ],
}


PRIVACY_REPLY = """Using general knowledge, here is a short customizable template:

Privacy Policy

Effective date: [Date]

[Website/Company Name] respects your privacy. We may collect information you
provide directly, such as your name, email address and messages, as well as
basic technical data such as your IP address and browser type.

We use this information to operate and improve our website and respond to
inquiries. We do not sell your personal information."""

# Nothing listens on the discard port: a model call fails at once.
NOWHERE = "http://127.0.0.1:9"


def main(port: int, archive: Path | None = None) -> None:
    url = os.environ["TEST_DATABASE_URL"]
    schema = f"browser_{uuid4().hex}"
    engine = make_engine(url)
    quoted = engine.dialect.identifier_preparer.quote(schema)
    config = Config(str(ROOT / "alembic.ini"))
    # An archive restores into the schema it was written at, then migrates on,
    # as a real office would after restoring an older backup.
    start_at = archive_revision(archive) if archive else "head"
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {quoted}"))
        connection.execute(text(f"SET search_path TO {quoted}"))
        connection.commit()
        config.attributes["connection"] = connection
        command.upgrade(config, start_at)
        connection.commit()
    try:
        with tempfile.TemporaryDirectory() as folder:
            # The workspace stays empty for a restore; the key lives beside it.
            workspace = Path(folder) / "workspace"
            workspace.mkdir()
            generate_root_key(Path(folder) / "root.key")
            settings = Settings(
                _env_file=None,
                database_url=f"{url}?options=-csearch_path%3D{schema}",
                owner_api_token=TOKEN,
                workspace_root=workspace,
                secret_key_file=Path(folder) / "root.key",
                backup_dir=Path(folder) / "backups",
            )
            seeded = restored(settings, archive) if archive else seed(settings)
            if archive:
                with engine.connect() as connection:
                    connection.execute(text(f"SET search_path TO {quoted}"))
                    config.attributes["connection"] = connection
                    command.upgrade(config, "head")
                    connection.commit()
            print(json.dumps({**seeded, "token": TOKEN}), flush=True)
            uvicorn.run(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    finally:
        with engine.connect() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {quoted} CASCADE"))
            connection.commit()
        engine.dispose()


def archive_revision(archive: Path) -> str:
    """The schema revision a backup archive was written at."""
    import tarfile

    with tarfile.open(archive) as bundle:
        member = bundle.extractfile("manifest.json")
        if member is None:
            raise SystemExit(f"{archive.name} has no manifest")
        return str(json.load(member)["schema_revision"])


def restored(settings: Settings, archive: Path) -> dict[str, str]:
    """A copy of the office the archive came from."""
    import asyncio

    url = settings.database_url.get_secret_value()

    async def checkpoint_tables() -> None:
        async with open_checkpointer(url):
            pass

    asyncio.run(checkpoint_tables())
    engine = make_engine(url)
    restore_backup(archive, engine, settings.workspace_root)
    engine.dispose()
    return {"restored_from": archive.name}


def seed(settings: Settings) -> dict[str, str]:
    engine = make_engine(settings.database_url.get_secret_value())
    factory = make_session_factory(engine)
    repository = RunRepository(factory, ArtifactStore(settings.workspace_root))
    with factory() as session, session.begin():
        project = Project(name="Northwind advisory")
        session.add(project)
        session.flush()
        project_id = project.id
    runs: dict[str, str] = {}
    for name, objective, payload, shape in (
        ("brief", "Client onboarding against the Wwft", BRIEF, "brief"),
        ("deck", "Quarterly update for the partners", DECK, "slides"),
        ("dashboard", "Sign-ups this year", DASHBOARD, "dashboard"),
    ):
        dispatch = repository.create(project_id, objective)
        repository.record_stage(dispatch, "synthesis", payload)
        with factory() as session, session.begin():
            session.execute(
                update(Run)
                .where(Run.id == dispatch.run_id)
                .values(
                    status="COMPLETED",
                    deliverable_json={
                        "shape": shape,
                        "format": "html" if shape == "dashboard" else "docx",
                    },
                )
            )
        runs[name] = str(dispatch.run_id)
    # Two providers, and one move to a backup, for the cards.
    with factory() as session, session.begin():
        for key, provider, name, model in (
            ("deepseek", "deepseek", "DeepSeek", "deepseek-flash"),
            ("haiku", "anthropic", "Haiku", "claude-haiku"),
        ):
            connection = ModelConnection(
                provider=provider,
                display_name=name,
                model_id=model,
                credential_ref=f"seeded-{key}",
                status="MODEL_AVAILABLE",
            )
            session.add(connection)
            session.flush()
            runs[key] = str(connection.id)
    repository.add_event(
        UUID(runs["brief"]),
        "model_failed_over",
        {
            "role": "researcher",
            "from": "DeepSeek",
            "to": "Haiku",
            "from_key": runs["deepseek"],
            "to_key": runs["haiku"],
            "reason": "no_credit",
        },
    )
    # A discussion with a long reply, to copy or open in the editor.
    with factory() as session, session.begin():
        topic = Topic(title="Privacy terms for the website")
        session.add(topic)
        session.flush()
        session.add(
            RoomMessage(
                topic_id=topic.id,
                author_kind="agent",
                author_role="chief",
                author_name="Tim",
                body=PRIVACY_REPLY,
            )
        )
        runs["topic"] = str(topic.id)
        # An agent's proposed call that asks the owner first.
        session.add(
            RoomMessage(
                topic_id=topic.id,
                author_kind="agent",
                author_role="researcher",
                author_name="Iris",
                body="I can put this on a board for you.",
                actions_json=[
                    {
                        "kind": "tools_used",
                        "calls": [
                            {"server": "Miro", "tool": "board_search_boards", "outcome": "ok"}
                        ],
                    },
                    {
                        "kind": "tool_call",
                        "server": "Miro",
                        "tool": "board_create",
                        "arguments": {"title": "Privacy"},
                    },
                ],
            )
        )
    # A question the lead answered, for the owner's verdict (3.0).
    with factory() as session, session.begin():
        asked_topic = Topic(title="Dutch VAT on software")
        session.add(asked_topic)
        session.flush()
        question = RoomMessage(
            topic_id=asked_topic.id,
            author_kind="owner",
            author_name="Alex",
            body="What VAT rate applies to software sold in the Netherlands?",
        )
        session.add(question)
        session.flush()
        answer = RoomMessage(
            topic_id=asked_topic.id,
            author_kind="agent",
            author_role="chief",
            author_name="Tim",
            body="21%, the standard rate, for software sold to Dutch customers.",
            reply_to=question.id,
        )
        session.add(answer)
        session.flush()
        runs["asked_topic"] = str(asked_topic.id)
        runs["answer"] = str(answer.id)
        asked_about = (asked_topic.id, question.id)
    # A room a routine feeds: ten mornings of recaps, the newest last.
    with factory() as session, session.begin():
        recaps = Topic(title="Morning recap")
        session.add(recaps)
        session.flush()
        first = datetime(2026, 10, 27, 7, 30, tzinfo=UTC)
        for day in range(10):
            at = first + timedelta(days=day)
            asked = RoomMessage(
                topic_id=recaps.id,
                author_kind="office",
                author_name="The office",
                body=f"Morning recap (a routine, not typed by me):\n{RECAP_ASKS}",
                client_key=f"routine:seeded:{int(at.timestamp())}",
                created_at=at,
            )
            session.add(asked)
            session.flush()
            session.add(
                RoomMessage(
                    topic_id=recaps.id,
                    author_kind="agent",
                    author_role="chief",
                    author_name="Tim",
                    body=f"Recap for {at:%d %B}.\n" + RECAP_REPLY,
                    reply_to=asked.id,
                    created_at=at + timedelta(minutes=2),
                )
            )
        runs["recap_topic"] = str(recaps.id)
    # The model call behind that reply, kept as the office keeps one.
    shaped, bodies = request_of(
        "chief", [{"role": "user", "content": "What VAT rate applies?"}], None, None, None
    )
    ModelCalls(factory).keep(
        None,
        "chief",
        shaped,
        bodies,
        Completion(
            model_alias="chief",
            model="seeded-model",
            content="21%, the standard rate.",
            usage=Usage(prompt_tokens=120, completion_tokens=12),
            latency_seconds=0.8,
        ),
        "ok",
        0.8,
        topic_id=asked_about[0],
        message_id=asked_about[1],
    )
    # A task stuck on a page that does not respond.
    stuck = repository.create(project_id, "Audit loot-drop.io")
    repository.merge_state(
        stuck.run_id,
        {
            "plan": {
                "research_question": "What should loot-drop.io do next?",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "browse",
                        "brief": "Read the case studies",
                        "depends_on": [],
                        "effort": "standard",
                    },
                    {
                        "id": "s2",
                        "kind": "write",
                        "brief": "Write the five actions",
                        "depends_on": ["s1"],
                        "effort": "standard",
                    },
                ],
            },
            "step_status": {"s1": "pending", "s2": "pending"},
        },
    )
    repository.stop(stuck.run_id, "PAUSED")
    repository.add_event(
        stuck.run_id,
        "stuck_ask",
        {
            "what": "click 'Case studies' on www.loot-drop.io",
            "error": "action_timed_out",
            "tries": 2,
            "step": "s1",
            "brief": "Read the case studies",
        },
    )
    runs["stuck"] = str(stuck.run_id)
    # A finished build: a small website in its project folder.
    from app import builds

    built = repository.create(project_id, "A colouring-page website")
    folder = builds.folder_for(settings, str(built.run_id))
    builds.ensure_repository(folder)
    import shutil
    import subprocess

    git = shutil.which("git") or "git"

    def version(files: dict[str, str], run: str, subject: str) -> str:
        for name, content in files.items():
            (folder / name).write_text(content)
        who = ["-c", "user.name=t", "-c", "user.email=t@t"]
        message = ["-m", subject, "-m", f"Office-Run: {run}"]
        subprocess.run([git, "-C", str(folder), "add", "-A"], check=True)  # noqa: S603
        subprocess.run([git, "-C", str(folder), *who, "commit", "-q", *message], check=True)  # noqa: S603
        head = subprocess.run(  # noqa: S603
            [git, "-C", str(folder), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return head.stdout.strip()

    # Two versions: this task made the first; a later one made the second.
    first = version(
        {"index.html": "<title>Colour</title><h1>Colour</h1>", "README.md": "Open index.html"},
        str(built.run_id),
        "Build: colouring pages",
    )
    version({"index.html": "<title>Colour 2</title><h1>Colour 2</h1>"}, str(uuid4()), "Build: v2")
    repository.merge_state(
        built.run_id,
        {
            "plan": {"research_question": "Build it", "task_kind": "build", "steps": []},
            "build_folder": str(built.run_id),
            "build_commit": first,
            "engineering": {
                "status": "completed",
                "landed": True,
                "errors": [],
                "summary": "Made the site.",
            },
        },
    )
    with factory() as session, session.begin():
        session.execute(update(Run).where(Run.id == built.run_id).values(status="COMPLETED"))
    runs["built"] = str(built.run_id)
    # A lead task that proposed a full team run, not yet started.
    proposing = repository.create(project_id, "A Slack self-check tool")
    repository.merge_state(
        proposing.run_id,
        {"synthesis": {"lead": "Feasible; a full team run is proposed.", "facts": []}},
    )
    repository.add_event(
        proposing.run_id,
        "team_run_proposed",
        {"objective": "Build the Slack self-check tool", "why": "It needs code written."},
    )
    with factory() as session, session.begin():
        session.execute(update(Run).where(Run.id == proposing.run_id).values(status="COMPLETED"))
    runs["proposing"] = str(proposing.run_id)
    # A Slack workspace connected by an app made before files and buttons.
    # No app-level token, so the office never opens a connection to Slack.
    with factory() as session, session.begin():
        app = ChannelApp(kind="slack")
        session.add(app)
        session.flush()
        installation = ChannelInstallation(
            app_id=app.id,
            kind="slack",
            workspace_id="T0SEEDED",
            workspace_name="Northwind",
            bot_user_id="U0BOT",
            token_ref="seeded-slack",
            scopes_json=["chat:write", "app_mentions:read", "im:history"],
            status="ACTIVE",
        )
        session.add(installation)
        session.flush()
        # A channel a standing report can be posted to, and a schedule to post.
        # The schedule is off: nothing here may start paid work.
        sales = ChannelDestination(
            installation_id=installation.id,
            external_id="C0SALES",
            name="#sales",
            events_json=[],
            enabled=True,
        )
        template = TaskTemplate(
            name="Competitor prices", project_id=project_id, objective="Check competitor prices"
        )
        session.add_all([sales, template])
        session.flush()
        session.add(
            Schedule(
                template_id=template.id,
                timezone="Europe/Amsterdam",
                frequency="weekly",
                local_time="09:00",
                weekday=0,
                enabled=False,
                disabled_reason="disabled_by_owner",
            )
        )
    engine.dispose()
    return runs


if __name__ == "__main__":
    main(int(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
