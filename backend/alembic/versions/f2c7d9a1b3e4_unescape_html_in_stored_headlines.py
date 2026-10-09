"""data: decode HTML entities in stored triage headlines

Benzinga headlines arrived HTML-escaped ("Apple&#39;s") and were stored as-is
until ingestion started unescaping them (2026-10). Decode the existing rows so
the News pane, ticker news and replay all see plain text. Idempotent for any
realistic headline (no double-escaped text in this feed).

Revision ID: f2c7d9a1b3e4
Revises: e81f4b2c9a70
Create Date: 2026-10-09 05:00:00.000000

"""
import html
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2c7d9a1b3e4'
down_revision: Union[str, Sequence[str], None] = 'e81f4b2c9a70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade data."""
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, headline, body_excerpt FROM triage_decisions "
            "WHERE headline LIKE '%&%;%' OR body_excerpt LIKE '%&%;%'"
        )
    ).fetchall()
    for row_id, headline, body in rows:
        conn.execute(
            sa.text("UPDATE triage_decisions SET headline = :h, body_excerpt = :b WHERE id = :i"),
            {
                "h": html.unescape(headline)[:300],
                "b": html.unescape(body) if body is not None else None,
                "i": row_id,
            },
        )


def downgrade() -> None:
    """Irreversible data cleanup (escaped text carries no extra information)."""
