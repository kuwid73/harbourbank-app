"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-27 16:27:27.409478
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('customers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('accounts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('account_number', sa.String(length=20), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=True),
    sa.Column('kind', sa.Enum('customer', 'internal', name='accountkind'), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('balance_cents', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.Enum('active', 'frozen', 'closed', name='accountstatus'), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind = 'internal' OR balance_cents >= 0", name='customer_balance_non_negative'),
    sa.CheckConstraint("kind = 'internal' OR customer_id IS NOT NULL", name='customer_account_has_owner'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_number')
    )
    op.create_table('transfers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.Enum('transfer', 'deposit', 'reversal', name='transferkind'), nullable=False),
    sa.Column('from_account_id', sa.Uuid(), nullable=False),
    sa.Column('to_account_id', sa.Uuid(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('reference', sa.String(length=140), nullable=False),
    sa.Column('status', sa.Enum('completed', 'rejected', 'reversed', name='transferstatus'), nullable=False),
    sa.Column('reject_reason', sa.Enum('insufficient_funds', 'account_frozen', 'account_closed', 'currency_mismatch', 'daily_limit_exceeded', name='rejectreason'), nullable=True),
    sa.Column('reversal_of_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount_cents > 0', name='amount_positive'),
    sa.ForeignKeyConstraint(['from_account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['reversal_of_id'], ['transfers.id'], ),
    sa.ForeignKeyConstraint(['to_account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key'),
    sa.UniqueConstraint('reversal_of_id')
    )
    op.create_index(op.f('ix_transfers_created_at'), 'transfers', ['created_at'], unique=False)
    op.create_index(op.f('ix_transfers_from_account_id'), 'transfers', ['from_account_id'], unique=False)
    op.create_index(op.f('ix_transfers_to_account_id'), 'transfers', ['to_account_id'], unique=False)
    op.create_table('ledger_entries',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('transfer_id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('balance_after_cents', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['transfer_id'], ['transfers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ledger_entries_account_created', 'ledger_entries', ['account_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_ledger_entries_transfer_id'), 'ledger_entries', ['transfer_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ledger_entries_transfer_id'), table_name='ledger_entries')
    op.drop_index('ix_ledger_entries_account_created', table_name='ledger_entries')
    op.drop_table('ledger_entries')
    op.drop_index(op.f('ix_transfers_to_account_id'), table_name='transfers')
    op.drop_index(op.f('ix_transfers_from_account_id'), table_name='transfers')
    op.drop_index(op.f('ix_transfers_created_at'), table_name='transfers')
    op.drop_table('transfers')
    op.drop_table('accounts')
    op.drop_table('customers')
    # Autogenerate doesn't drop Postgres enum types; without this, upgrade-after-downgrade fails
    for enum_name in ('rejectreason', 'transferstatus', 'transferkind', 'accountstatus', 'accountkind'):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
