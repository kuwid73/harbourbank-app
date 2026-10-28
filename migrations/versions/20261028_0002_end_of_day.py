"""end of day: interest transfers and eod_runs

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28 16:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres can add an enum value but never remove one; IF NOT EXISTS keeps re-upgrades safe.
    # The new value may not be used in this same transaction - nothing here does.
    op.execute("ALTER TYPE transferkind ADD VALUE IF NOT EXISTS 'interest'")
    op.create_table('eod_runs',
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('status', sa.Enum('running', 'ok', 'failed', name='eodstatus'), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('accounts_credited', sa.Integer(), nullable=False),
    sa.Column('interest_cents', sa.BigInteger(), nullable=False),
    sa.Column('imbalance_cents', sa.BigInteger(), nullable=False),
    sa.Column('inconsistencies', sa.Integer(), nullable=False),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.PrimaryKeyConstraint('business_date')
    )


def downgrade() -> None:
    op.drop_table('eod_runs')
    sa.Enum(name='eodstatus').drop(op.get_bind(), checkfirst=True)
    # 'interest' stays in transferkind (Postgres cannot drop enum values). The 0001 code cannot
    # read interest transfers, so this downgrade is only safe before the first end-of-day run.
