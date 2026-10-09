"""add thesis_attempts table (grounding pass-rate denominator)

Revision ID: e81f4b2c9a70
Revises: d3a91c7e5f20
Create Date: 2026-10-09 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e81f4b2c9a70'
down_revision: Union[str, Sequence[str], None] = 'd3a91c7e5f20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('thesis_attempts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('source_bucket', sa.String(length=16), nullable=False),
    sa.Column('outcome', sa.String(length=24), nullable=False),
    sa.Column('correlation_id', sa.String(length=32), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('llm_provider', sa.String(length=32), nullable=True),
    sa.Column('llm_model', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_thesis_attempts_attempted_at'), 'thesis_attempts', ['attempted_at'], unique=False)
    op.create_index(op.f('ix_thesis_attempts_source_bucket'), 'thesis_attempts', ['source_bucket'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_thesis_attempts_source_bucket'), table_name='thesis_attempts')
    op.drop_index(op.f('ix_thesis_attempts_attempted_at'), table_name='thesis_attempts')
    op.drop_table('thesis_attempts')
