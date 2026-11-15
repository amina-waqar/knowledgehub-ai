# database.py
"""
Database engine and session management.
"""
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from config import settings

def normalize_database_url(url: str) -> str:
    """
    Accept the URL forms Neon and other hosts hand out.

    Neon shows `postgresql://user:pass@host/db?sslmode=require`. SQLAlchemy
    needs the driver named explicitly to use psycopg 3, so rewrite the scheme.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = normalize_database_url(settings.DATABASE_URL)


def _is_neon_pooled(url: str) -> bool:
    # Neon's pooled endpoint has "-pooler" in the host name (PgBouncer in
    # transaction mode, which does not support server-side prepared statements).
    return "-pooler" in url.split("@")[-1].split("/")[0]


# SQLite needs check_same_thread=False because uvicorn serves requests from
# multiple threads. Postgres has no such restriction and instead benefits from
# connection pooling settings.
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    _connect_args = {"connect_timeout": 20}
    if _is_neon_pooled(DATABASE_URL):
        _connect_args["prepare_threshold"] = None  # required behind PgBouncer
    engine = create_engine(
        DATABASE_URL,
        # Small pool: Neon Free has a connection limit and scales to zero.
        pool_size=3,
        max_overflow=4,
        pool_pre_ping=True,   # drops connections the idle compute already closed
        pool_recycle=240,     # recycle before Neon's idle suspend (~5 min)
        connect_args=_connect_args,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_database():
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def database_size_mb() -> float | None:
    """Total size of the current Postgres database in MB (None on SQLite)."""
    if not settings.is_postgres:
        return None
    with engine.connect() as conn:
        size = conn.execute(text("SELECT pg_database_size(current_database())")).scalar()
    return round((size or 0) / (1024 * 1024), 2)


def wait_for_database(max_attempts: int = 30, delay_seconds: float = 2.0) -> None:
    """
    Block until the database accepts connections.

    Needed in Docker (Postgres starts after the app) and on Neon Free, whose
    compute suspends when idle and takes a few seconds to wake.
    """
    if DATABASE_URL.startswith("sqlite"):
        return

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print(f"[db] Connected to database on attempt {attempt}.")
            return
        except OperationalError as exc:
            last_error = exc
            print(f"[db] Not ready (attempt {attempt}/{max_attempts}), retrying...")
            time.sleep(delay_seconds)

    raise RuntimeError(f"Database never became available: {last_error}")


def ensure_pgvector_extension() -> None:
    """
    Create the pgvector extension if we are on Postgres and using it.

    Must run before create_all(), because the vector column type does not exist
    until the extension is installed.
    """
    if not settings.use_pgvector:
        return

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    print("[db] pgvector extension is available.")


def create_vector_index() -> None:
    """
    Add an approximate-nearest-neighbour index on the embedding column.

    Without this, Postgres does an exact scan of every chunk. With it, retrieval
    stays fast as the runbook corpus grows. HNSW gives better recall than
    ivfflat and does not need a pre-populated table to build.
    """
    if not settings.use_pgvector:
        return

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw "
                "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
            )
        )
    print("[db] Vector index ready.")