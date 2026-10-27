from alembic import command

from conftest import alembic_config


def test_models_and_migrations_agree(database_url):
    # Fails if someone changes a model without adding a migration (or vice versa)
    command.check(alembic_config(database_url))


def test_downgrade_then_upgrade_round_trips(database_url):
    cfg = alembic_config(database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
