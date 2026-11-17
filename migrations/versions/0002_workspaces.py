"""Workspaces, memberships, and an ownership-based backfill.

Every existing user gets a personal workspace they own. Incidents follow their
creator, documents their uploader, and questions/feedback the incident they are
about. Rows with no resolvable owner go to a Legacy workspace that only global
admins can open. No rows are deleted.

Revision ID: 0002_workspaces
Revises: 0001_baseline
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_workspaces"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

SCOPED_TABLES = ["incidents", "documents", "question_logs", "answer_feedback"]
LEGACY_WORKSPACE_NAME = "Legacy (unassigned)"

workspaces = sa.table(
    "workspaces", sa.column("id", sa.Integer), sa.column("name", sa.String), sa.column("created_by", sa.Integer)
)
workspace_members = sa.table(
    "workspace_members",
    sa.column("workspace_id", sa.Integer),
    sa.column("user_id", sa.Integer),
    sa.column("role", sa.String),
)


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("ix_workspaces_id", "workspaces", ["id"])

    op.create_table(
        "workspace_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
    )
    op.create_index("ix_workspace_members_id", "workspace_members", ["id"])
    op.create_index("ix_workspace_members_workspace_id", "workspace_members", ["workspace_id"])
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    for table in SCOPED_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))

    backfill(op.get_bind())

    for table in SCOPED_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column("workspace_id", existing_type=sa.Integer(), nullable=False)
            batch.create_foreign_key(f"fk_{table}_workspace_id", "workspaces", ["workspace_id"], ["id"])
            batch.create_index(f"ix_{table}_workspace_id", ["workspace_id"])


def backfill(bind) -> None:
    personal_workspaces = {}
    users = bind.execute(sa.text("SELECT id, username FROM users ORDER BY id")).all()
    for user_id, username in users:
        workspace_id = bind.execute(
            workspaces.insert()
            .values(name=f"{username}'s workspace", created_by=user_id)
            .returning(workspaces.c.id)
        ).scalar_one()
        bind.execute(workspace_members.insert().values(workspace_id=workspace_id, user_id=user_id, role="owner"))
        personal_workspaces[user_id] = workspace_id

    owners = [{"workspace_id": w, "user_id": u} for u, w in personal_workspaces.items()]
    if owners:
        bind.execute(sa.text("UPDATE incidents SET workspace_id = :workspace_id WHERE creator_id = :user_id"), owners)
        bind.execute(sa.text("UPDATE documents SET workspace_id = :workspace_id WHERE uploader_id = :user_id"), owners)

    for table in ("question_logs", "answer_feedback"):
        bind.execute(sa.text(
            f"UPDATE {table} SET workspace_id = "
            f"(SELECT incidents.workspace_id FROM incidents WHERE incidents.id = {table}.incident_id)"
        ))

    cross_user_questions = bind.execute(sa.text(
        "SELECT count(*) FROM question_logs q JOIN incidents i ON i.id = q.incident_id "
        "WHERE q.user_id IS NOT NULL AND q.user_id <> i.creator_id"
    )).scalar_one()

    unassigned = {
        table: bind.execute(sa.text(f"SELECT count(*) FROM {table} WHERE workspace_id IS NULL")).scalar_one()
        for table in SCOPED_TABLES
    }
    if any(unassigned.values()):
        legacy_id = bind.execute(
            workspaces.insert().values(name=LEGACY_WORKSPACE_NAME, created_by=None).returning(workspaces.c.id)
        ).scalar_one()
        admin_ids = bind.execute(sa.text("SELECT id FROM users WHERE is_admin = :yes"), {"yes": True}).scalars().all()
        for admin_id in admin_ids:
            bind.execute(workspace_members.insert().values(workspace_id=legacy_id, user_id=admin_id, role="admin"))
        for table in SCOPED_TABLES:
            bind.execute(
                sa.text(f"UPDATE {table} SET workspace_id = :legacy_id WHERE workspace_id IS NULL"),
                {"legacy_id": legacy_id},
            )

    print(f"[migrate] Created {len(personal_workspaces)} personal workspace(s).")
    print(f"[migrate] Rows moved to {LEGACY_WORKSPACE_NAME}: {unassigned}")
    print(f"[migrate] Questions asked about another user's incident (kept with the incident): {cross_user_questions}")


def downgrade() -> None:
    # Removes workspace assignments only; every incident, document, question and
    # feedback row is kept.
    for table in reversed(SCOPED_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_workspace_id")
            batch.drop_constraint(f"fk_{table}_workspace_id", type_="foreignkey")
            batch.drop_column("workspace_id")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
