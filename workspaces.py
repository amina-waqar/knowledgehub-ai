"""Workspace membership, role checks, and the endpoints that manage workspaces."""
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from auth import get_current_user, get_user_by_username
from database import DbSessionDep

Role = models.WorkspaceRole
ROLE_RANK = {Role.VIEWER: 0, Role.MEMBER: 1, Role.ADMIN: 2, Role.OWNER: 3}

CurrentUser = Annotated[models.User, Depends(get_current_user)]

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class MemberAdd(BaseModel):
    username: str
    role: Literal["admin", "member", "viewer"] = "member"


def create_personal_workspace(db: Session, user: models.User) -> models.Workspace:
    """Add (but do not commit) a workspace owned by this user."""
    workspace = models.Workspace(name=f"{user.username}'s workspace", created_by=user.id)
    workspace.members.append(models.WorkspaceMember(user=user, role=Role.OWNER))
    db.add(workspace)
    return workspace


def find_membership(db: Session, user_id: int, workspace_id: int) -> Optional[models.WorkspaceMember]:
    return (
        db.query(models.WorkspaceMember)
        .filter(
            models.WorkspaceMember.user_id == user_id,
            models.WorkspaceMember.workspace_id == workspace_id,
        )
        .first()
    )


def membership_or_404(db: Session, user: models.User, workspace_id: int) -> models.WorkspaceMember:
    # Non-members get the same 404 as a missing workspace, so ids can't be probed.
    membership = find_membership(db, user.id, workspace_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return membership


def get_membership(
    current_user: CurrentUser,
    db: DbSessionDep,
    x_workspace_id: Annotated[Optional[int], Header()] = None,
) -> models.WorkspaceMember:
    """
    The workspace a request acts on, taken from the X-Workspace-Id header.
    Without the header the user's first workspace is used, so older clients
    keep working.
    """
    if x_workspace_id is not None:
        return membership_or_404(db, current_user, x_workspace_id)

    membership = (
        db.query(models.WorkspaceMember)
        .filter(models.WorkspaceMember.user_id == current_user.id)
        .order_by(models.WorkspaceMember.workspace_id)
        .first()
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return membership


MembershipDep = Annotated[models.WorkspaceMember, Depends(get_membership)]


def require_role(membership: models.WorkspaceMember, minimum: str) -> None:
    if ROLE_RANK.get(membership.role, -1) < ROLE_RANK[minimum]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This action requires the {minimum} role in this workspace.",
        )


def _workspace_to_dict(membership: models.WorkspaceMember) -> dict:
    return {
        "id": membership.workspace_id,
        "name": membership.workspace.name,
        "role": membership.role,
    }


def _member_to_dict(membership: models.WorkspaceMember) -> dict:
    return {
        "user_id": membership.user_id,
        "username": membership.user.username,
        "role": membership.role,
    }


@router.get("")
def list_workspaces(current_user: CurrentUser, db: DbSessionDep):
    memberships = (
        db.query(models.WorkspaceMember)
        .filter(models.WorkspaceMember.user_id == current_user.id)
        .order_by(models.WorkspaceMember.workspace_id)
        .all()
    )
    return {"workspaces": [_workspace_to_dict(m) for m in memberships]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_workspace(payload: WorkspaceCreate, current_user: CurrentUser, db: DbSessionDep):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Name cannot be blank.")

    workspace = models.Workspace(name=name, created_by=current_user.id)
    membership = models.WorkspaceMember(user=current_user, role=Role.OWNER)
    workspace.members.append(membership)
    db.add(workspace)
    db.commit()
    db.refresh(membership)
    return _workspace_to_dict(membership)


@router.get("/{workspace_id}/members")
def list_members(workspace_id: int, current_user: CurrentUser, db: DbSessionDep):
    membership_or_404(db, current_user, workspace_id)
    members = (
        db.query(models.WorkspaceMember)
        .filter(models.WorkspaceMember.workspace_id == workspace_id)
        .order_by(models.WorkspaceMember.id)
        .all()
    )
    return {"members": [_member_to_dict(m) for m in members]}


@router.post("/{workspace_id}/members", status_code=status.HTTP_201_CREATED)
def add_member(workspace_id: int, payload: MemberAdd, current_user: CurrentUser, db: DbSessionDep):
    me = membership_or_404(db, current_user, workspace_id)
    require_role(me, Role.ADMIN)
    if payload.role == Role.ADMIN:
        require_role(me, Role.OWNER)

    user = get_user_by_username(db, payload.username)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if find_membership(db, user.id, workspace_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member.")

    membership = models.WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role=payload.role)
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return _member_to_dict(membership)


@router.delete("/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(workspace_id: int, user_id: int, current_user: CurrentUser, db: DbSessionDep):
    me = membership_or_404(db, current_user, workspace_id)
    require_role(me, Role.ADMIN)

    target = find_membership(db, user_id, workspace_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")
    if target.role == Role.OWNER:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The workspace owner cannot be removed.")
    if target.role == Role.ADMIN:
        require_role(me, Role.OWNER)

    db.delete(target)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
