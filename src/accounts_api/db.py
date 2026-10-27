from sqlalchemy import Engine, create_engine

from .config import Settings


def make_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        # No overflow: when the pool is exhausted, requests wait db_pool_timeout_seconds
        # and then fail with a clear error, instead of opening unbounded connections
        max_overflow=0,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
    )
