import os
import sys

from alembic import context
from sqlalchemy import create_engine

from accounts_api.config import normalize_db_url
from accounts_api.logging_setup import configure_logging
from accounts_api.models import Base

config = context.config
# Same JSON log format as the app, so the migration Job's output is searchable in Loki
configure_logging("INFO")
target_metadata = Base.metadata


def database_url() -> str:
    # Tests pass the URL programmatically; deployments use the environment
    if url := config.attributes.get("database_url"):
        return url
    if "DATABASE_URL" not in os.environ:
        sys.exit("configuration error: environment variable 'DATABASE_URL' is required but not set")
    return normalize_db_url(os.environ["DATABASE_URL"])


def run_offline() -> None:
    """Emit SQL without connecting (alembic upgrade head --sql), e.g. for DBA review."""
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
