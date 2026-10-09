"""add llm_calls table (durable per-call LLM usage log)

Revision ID: d3a91c7e5f20
Revises: b507243bb597
Create Date: 2026-10-09 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3a91c7e5f20'
down_revision: Union[str, Sequence[str], None] = 'b507243bb597'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('llm_calls',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('called_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('step', sa.String(length=16), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('method', sa.String(length=24), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('error_reason', sa.String(length=32), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('correlation_id', sa.String(length=32), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_llm_calls_called_at'), 'llm_calls', ['called_at'], unique=False)
    op.create_index(op.f('ix_llm_calls_step'), 'llm_calls', ['step'], unique=False)
    op.create_index('idx_llm_calls_provider_model_at', 'llm_calls', ['provider', 'model', 'called_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_llm_calls_provider_model_at', table_name='llm_calls')
    op.drop_index(op.f('ix_llm_calls_step'), table_name='llm_calls')
    op.drop_index(op.f('ix_llm_calls_called_at'), table_name='llm_calls')
    op.drop_table('llm_calls')
