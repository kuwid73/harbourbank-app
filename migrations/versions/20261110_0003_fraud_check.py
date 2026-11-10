"""fraud check: decision on each transfer, two reject reasons

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10 09:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

fraudcheck = sa.Enum('allow', 'block', 'unavailable', name='fraudcheck')


def upgrade() -> None:
    # Postgres can add an enum value but never remove one; IF NOT EXISTS keeps re-upgrades safe.
    op.execute("ALTER TYPE rejectreason ADD VALUE IF NOT EXISTS 'fraud_suspected'")
    op.execute("ALTER TYPE rejectreason ADD VALUE IF NOT EXISTS 'fraud_check_unavailable'")
    fraudcheck.create(op.get_bind(), checkfirst=True)
    # Nullable, no default: existing transfers were never checked, and adding it is instant
    op.add_column('transfers', sa.Column('fraud_check', fraudcheck, nullable=True))


def downgrade() -> None:
    op.drop_column('transfers', 'fraud_check')
    fraudcheck.drop(op.get_bind(), checkfirst=True)
    # The two reject reasons stay (Postgres cannot drop enum values). The 0002 code cannot read
    # transfers rejected with them, so this downgrade is only safe before fraud-check is switched on.
