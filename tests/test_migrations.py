from alembic import command
from sqlalchemy import create_engine

from tests.conftest import alembic_config


def test_upgrade_and_downgrade_roundtrip(test_database_url):
    config = alembic_config(test_database_url)
    engine = create_engine(test_database_url)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        with engine.connect() as conn:
            version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        assert version  # at head
        command.downgrade(config, "base")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM alembic_version").scalar_one() == 0
    finally:
        command.upgrade(config, "head")  # leave the shared test database migrated
        engine.dispose()


def test_models_match_migrations(test_database_url):
    """`alembic check` fails if models changed without a migration."""
    config = alembic_config(test_database_url)
    command.upgrade(config, "head")
    command.check(config)
