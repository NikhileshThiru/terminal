"""add pre_strictness flag and seen_discovery_events table

Revision ID: 68b247b4a5dd
Revises: fee6c336e464
Create Date: 2026-06-04 16:35:47.328356

Two changes:
1. `seen_discovery_events` — persistent dedup table for the discovery
   pipeline. The in-memory set in EdgarPoller was process-local; a restart
   would silently swallow any filing that arrived during downtime.
2. `theses.pre_strictness` — marks theses produced before the Phase 4.5
   strictness fixes (orchestrator preconditions + grounding hard-reject).
   Backfilled to TRUE for any pre-existing row with grounding_check_passed
   = FALSE, since post-Phase-4.5 no such row can be persisted (CopilotError
   is raised before write_thesis is called). Excluded from dashboards.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "68b247b4a5dd"
down_revision: Union[str, Sequence[str], None] = "fee6c336e464"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "seen_discovery_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id", name="uq_seen_discovery_source_id"),
    )
    op.create_index(
        op.f("ix_seen_discovery_events_external_id"),
        "seen_discovery_events",
        ["external_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_seen_discovery_events_source"),
        "seen_discovery_events",
        ["source"],
        unique=False,
    )

    # server_default=false() so existing rows backfill to False without a
    # NOT NULL violation. The default is NOT dropped — we want the column
    # to default to False at the DB level too, so application code that
    # forgets to set it still produces the correct value.
    op.add_column(
        "theses",
        sa.Column(
            "pre_strictness",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        op.f("ix_theses_pre_strictness"), "theses", ["pre_strictness"], unique=False
    )

    # Backfill: any existing thesis with grounding_check_passed = FALSE was
    # produced by the pre-strictness orchestrator (it could persist failed
    # groundings; current orchestrator raises CopilotError instead).
    op.execute(
        "UPDATE theses SET pre_strictness = TRUE WHERE grounding_check_passed = FALSE"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_theses_pre_strictness"), table_name="theses")
    op.drop_column("theses", "pre_strictness")
    op.drop_index(
        op.f("ix_seen_discovery_events_source"), table_name="seen_discovery_events"
    )
    op.drop_index(
        op.f("ix_seen_discovery_events_external_id"), table_name="seen_discovery_events"
    )
    op.drop_table("seen_discovery_events")
