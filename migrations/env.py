from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import models  # noqa: F401 - registers every table on Base.metadata
from config import settings
from database import Base

config = context.config

if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# migrate.py passes the URL through attributes rather than set_main_option,
# because configparser would choke on '%' in URL-encoded passwords.
database_url = config.attributes.get("database_url") or settings.DATABASE_URL

engine = create_engine(database_url, poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        # SQLite can't ALTER most columns in place; batch mode rebuilds the table instead.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
