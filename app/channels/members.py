"""Who may talk to the office's bots, per workspace (see ChannelMember)."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import ChannelMember

OWNER = "owner"


def owner_of(session: Session, kind: str, workspace_id: str) -> str | None:
    return session.scalar(
        select(ChannelMember.external_user_id).where(
            ChannelMember.kind == kind,
            ChannelMember.workspace_id == workspace_id,
            ChannelMember.role == OWNER,
        )
    )


def set_owner(session: Session, kind: str, workspace_id: str, user_id: str | None) -> None:
    """Replace the workspace's owner; None removes it (before re-pairing)."""
    session.execute(
        delete(ChannelMember).where(
            ChannelMember.kind == kind,
            ChannelMember.workspace_id == workspace_id,
            (ChannelMember.role == OWNER) | (ChannelMember.external_user_id == user_id),
        )
    )
    if user_id:
        session.add(
            ChannelMember(
                kind=kind, workspace_id=workspace_id, external_user_id=user_id, role=OWNER
            )
        )
    session.flush()


MEMBER = "member"


def role_of(session: Session, kind: str, workspace_id: str, user_id: str) -> str | None:
    """The role this person has in this workspace, or None if they have none.

    Everyone else is a stranger: the bots answer them nothing at all, so a
    workspace can hold an office bot without telling the room it exists.
    """
    return session.scalar(
        select(ChannelMember.role).where(
            ChannelMember.kind == kind,
            ChannelMember.workspace_id == workspace_id,
            ChannelMember.external_user_id == user_id,
        )
    )


def members_of(session: Session, kind: str, workspace_id: str) -> list[dict[str, str]]:
    rows = session.scalars(
        select(ChannelMember)
        .where(ChannelMember.kind == kind, ChannelMember.workspace_id == workspace_id)
        .order_by(ChannelMember.role, ChannelMember.created_at)
    )
    return [{"id": str(row.id), "user_id": row.external_user_id, "role": row.role} for row in rows]


def add_member(session: Session, kind: str, workspace_id: str, user_id: str) -> bool:
    """Let one more person use the office's bots. Never touches the owner.

    Returns False if they already have a role, so adding someone twice, or
    trying to demote the owner this way, changes nothing.
    """
    if role_of(session, kind, workspace_id, user_id) is not None:
        return False
    session.add(
        ChannelMember(kind=kind, workspace_id=workspace_id, external_user_id=user_id, role=MEMBER)
    )
    session.flush()
    return True


def remove_member(session: Session, kind: str, workspace_id: str, user_id: str) -> bool:
    """Take someone's access away. The owner cannot be removed this way."""
    removed = session.execute(
        delete(ChannelMember)
        .where(
            ChannelMember.kind == kind,
            ChannelMember.workspace_id == workspace_id,
            ChannelMember.external_user_id == user_id,
            ChannelMember.role == MEMBER,
        )
        .returning(ChannelMember.id)
    ).first()
    return removed is not None
