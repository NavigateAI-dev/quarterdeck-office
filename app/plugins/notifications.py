"""Tell the owner when a run settles, on the channels they chose.

Was a direct call from the run engine; now a plugin listening for `RunSettled`,
so the engine no longer imports the channels that deliver it.
"""

from app.api.report_pages import page_path
from app.artifacts import ArtifactStore
from app.channels.notifications import notify_run
from app.config import Settings
from app.kernel import Scope
from app.runtime.events import RunSettled


class NotificationsPlugin:
    name: str = "notifications"
    requires: tuple[str, ...] = ("events", "session_factory", "settings")

    def setup(self, scope: Scope) -> None:
        settings: Settings = scope.get("settings")
        session_factory = scope.get("session_factory")
        artifacts = ArtifactStore(settings.workspace_root)

        def queue(event: RunSettled) -> None:
            notify_run(
                session_factory,
                event.run_id,
                event.status,
                artifacts,
                # The answer as a page of its own, linked from Slack.
                lambda artifact_id: page_path(settings, str(artifact_id)),
            )

        scope.defer(scope.get("events").subscribe(RunSettled, queue))
