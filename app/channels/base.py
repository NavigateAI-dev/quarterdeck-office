"""Channel abstraction for chat platforms (Slack now; Teams, Discord, Lark later).

An adapter knows one platform's installation, verification, destinations and
sending. The rest of the office talks only to this interface. Credentials are
held in the encrypted secret store by reference; agents never receive them,
and messages are sent by the application, never by an agent.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


class ChannelError(Exception):
    """A platform failure, as a code. Never carries a token or message body."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Installation:
    workspace_id: str
    workspace_name: str
    bot_user_id: str
    # Held only until it is written to the secret store.
    bot_token: str = field(default="", repr=False)
    scopes: tuple[str, ...] = ()
    # The platform user who installed the app: the verified owner mapping.
    owner_external_id: str | None = None


@dataclass(frozen=True)
class Destination:
    external_id: str
    name: str
    # Whether the bot can post there now (for example, it was invited).
    can_post: bool


@dataclass(frozen=True)
class OutboundMessage:
    text: str
    # Reply inside this thread, when the platform has threads.
    thread: str | None = None
    # Post under an agent's name and icon, where the platform allows it.
    username: str | None = None
    icon_emoji: str | None = None
    # Slack's Block Kit layout, buttons included; the text is then what a
    # notification or a screen reader shows.
    blocks: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class DeliveryResult:
    external_ref: str


class ChannelAdapter(Protocol):
    kind: str

    async def find_message(
        self, token: str, destination: str, text: str, since: float
    ) -> str | None:
        """The platform's own record of a message, if it can be read.

        Returns the message's reference when the platform shows it was
        delivered, None when it shows it was not, and raises ChannelError
        when the platform cannot answer — which is not the same as "no".
        """
        ...

    async def verify_token(self, token: str) -> Installation: ...

    async def list_destinations(self, token: str) -> list[Destination]: ...

    async def send(
        self, token: str, destination: str, message: OutboundMessage
    ) -> DeliveryResult: ...

    async def update(self, token: str, destination: str, ts: str, message: OutboundMessage) -> None:
        """Replace a message the office posted, in place."""
        ...

    async def upload(
        self, token: str, destination: str, thread: str | None, name: str, data: bytes
    ) -> None:
        """Share a file into a conversation, in a thread when given one."""
        ...

    async def download(self, token: str, url: str, limit: int) -> bytes:
        """A file a person shared, by the platform's own address, at most `limit` bytes."""
        ...

    async def message_text(self, token: str, channel: str, ts: str) -> str:
        """The words of one message the owner marked as an idea."""
        ...

    async def whisper(self, token: str, channel: str, user: str, text: str) -> None:
        """A message only that person sees."""
        ...
