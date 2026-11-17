from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from database import Base
from migrate import BASELINE_REVISION, BASELINE_SCHEMA, alembic_config


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


def build_pre_alembic_database(url: str) -> None:
    """The baseline schema with no version record, like a database made by create_all."""
    command.upgrade(alembic_config(url), BASELINE_REVISION)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
    engine.dispose()


def insert_rows(url: str, rows: dict[str, list[dict]]) -> None:
    engine = create_engine(url)
    with engine.begin() as connection:
        for table, items in rows.items():
            for item in items:
                columns = ", ".join(item)
                values = ", ".join(f":{name}" for name in item)
                connection.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({values})"), item)
    engine.dispose()


def snapshot(url: str) -> dict[str, list[tuple]]:
    """Every baseline column of every row, so before/after can be compared exactly."""
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return {
                table: [
                    tuple(row)
                    for row in connection.execute(
                        text(f'SELECT {", ".join(sorted(columns))} FROM "{table}" ORDER BY id')
                    )
                ]
                for table, columns in BASELINE_SCHEMA.items()
            }
    finally:
        engine.dispose()


def schema_differences(url: str) -> list:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()


def reset_postgres(url: str) -> None:
    # Wipes the whole public schema, so refuse anything that isn't clearly a test database.
    database = make_url(url).database or ""
    if "test" not in database:
        raise RuntimeError(f"Refusing to reset '{database}': TEST_POSTGRES_URL must name a test database.")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
