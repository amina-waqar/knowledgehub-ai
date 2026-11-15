from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, status, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware  # connects origins (React dev server, FastAPI)
from contextlib import asynccontextmanager
from typing import Annotated, Optional, TYPE_CHECKING
import hashlib
import os
import uuid
import re

from database import (
    engine,
    Base,
    get_database,
    SessionLocal,
    wait_for_database,
    ensure_pgvector_extension,
    create_vector_index,
    database_size_mb,
)
from sqlalchemy import func, select, or_, insert
from sqlalchemy.orm import Session
import models
import log_parsing
import log_matching
from log_sources import DatabaseLogSource
from migrations import add_missing_columns, backfill_default_workspace
from ratelimit import limiter, client_ip
import retention

from pypdf import PdfReader  # for PDF parsing

if TYPE_CHECKING:  # imported lazily so tests and tooling can load the app without torch
    from sentence_transformers import SentenceTransformer
import json
import asyncio

from passlib.context import CryptContext
from jose import jwt, JWTError
from datetime import timedelta, datetime, timezone

from pydantic import BaseModel, EmailStr, Field
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import settings
import llm

# Auth config now comes from the environment. Nothing secret is in this file.
SECRET_KEY = settings.SECRET_KEY
ALGORITHM = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES


def bootstrap_admin() -> None:
    """
    Create the first admin account from environment variables, once.

    This replaces the old /debug/create-user endpoint, which created an admin
    with a password that was readable in the source. Anyone who found the
    deployed API could have logged in as that admin and uploaded documents.
    """
    if not (settings.BOOTSTRAP_ADMIN_USERNAME and settings.BOOTSTRAP_ADMIN_PASSWORD):
        return

    db = SessionLocal()
    try:
        existing = (
            db.query(models.User)
            .filter(models.User.username == settings.BOOTSTRAP_ADMIN_USERNAME)
            .first()
        )
        if existing:
            return

        admin = models.User(
            username=settings.BOOTSTRAP_ADMIN_USERNAME,
            email=settings.BOOTSTRAP_ADMIN_EMAIL
            or f"{settings.BOOTSTRAP_ADMIN_USERNAME}@loglens.local",
            hashed_password=get_password_hash(settings.BOOTSTRAP_ADMIN_PASSWORD),
            is_admin=True,
        )
        db.add(admin)
        db.commit()
        print(f"[bootstrap] Created admin user '{admin.username}'.")
    finally:
        db.close()

def bootstrap_demo_user() -> None:
    """Create a standard demo account from environment variables, once."""
    if not (settings.DEMO_USERNAME and settings.DEMO_PASSWORD):
        return

    db = SessionLocal()
    try:
        existing = (
            db.query(models.User)
            .filter(models.User.username == settings.DEMO_USERNAME)
            .first()
        )

        if existing:
            return

        demo_user = models.User(
            username=settings.DEMO_USERNAME,
            email=settings.DEMO_EMAIL
            or f"{settings.DEMO_USERNAME}@loglens.local",
            hashed_password=get_password_hash(settings.DEMO_PASSWORD),
            is_admin=False,
        )

        db.add(demo_user)
        db.commit()
        print(f"[bootstrap] Created demo user '{demo_user.username}'.")
    finally:
        db.close()
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup work, in the order it has to happen.

    In Docker the app container starts before Postgres is ready, so we wait.
    pgvector has to be installed before create_all(), because the vector column
    type does not exist until the extension does. The index comes after the
    table exists.
    """
    wait_for_database()
    ensure_pgvector_extension()
    Base.metadata.create_all(bind=engine)
    add_missing_columns()          # columns/indexes added after first deploy
    create_vector_index()
    bootstrap_admin()
    bootstrap_demo_user()
    backfill_default_workspace()   # puts pre-workspace users/incidents in a workspace

    # Warm the embedding model so the first upload is not slow.
    await asyncio.to_thread(get_embedding_model)

    retention_task = asyncio.create_task(_retention_loop())

    print(f"[startup] DB: {settings.DATABASE_URL.split('@')[-1]}")
    print(f"[startup] pgvector: {settings.use_pgvector}")
    print(f"[startup] LLM provider: {settings.LLM_PROVIDER}")
    print(f"[startup] signup enabled: {settings.SIGNUP_ENABLED}")
    print(f"[startup] log retention: {settings.LOG_RETENTION_DAYS} days")
    try:
        yield
    finally:
        retention_task.cancel()


async def _retention_loop() -> None:
    """Purge expired logs shortly after startup and then on a timer."""
    await asyncio.sleep(30)
    while True:
        try:
            result = await asyncio.to_thread(retention.purge_old_logs)
            if result.get("entries_deleted"):
                print(f"[retention] {result}")
        except Exception as exc:  # noqa: BLE001 - never let housekeeping crash the app
            print(f"[retention] failed: {exc}")
        await asyncio.sleep(max(1, settings.RETENTION_INTERVAL_HOURS) * 3600)


app = FastAPI(
    title="LogLens AI",
    description="AI-powered incident investigation and log analysis assistant.",
    version="0.2.0",
    lifespan=lifespan,
)

_cors_origins = ["http://localhost:5173"] + settings.cors_origin_list  # 5173 = React dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ensuring upload directory exists
UPLOAD_DIR = "uploaded_files"
os.makedirs(UPLOAD_DIR, exist_ok=True)

DbSessionDep = Annotated[Session, Depends(get_database)]
# Annotated[Session, Depends(get_database)] tells FastAPI:
# “this parameter is a Session and should be provided by get_database.”

#Pydantic Schemas (for requests/responses)

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_.\-]+$")
    email: EmailStr
    # bcrypt ignores bytes past 72, so cap there rather than silently truncate.
    password: str = Field(..., min_length=10, max_length=72)


class UserLogin(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class AnswerFeedbackSubmit(BaseModel):
    question_log_id: int
    is_helpful: bool
    feedback_text: Optional[str] = None


class IncidentCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    description: Optional[str] = None
    severity: Optional[str] = None
    status: str = "open"
    # When the incident began. Anchors the log search window. Optional.
    started_at: Optional[datetime] = None
    # Primary affected service, matched against the `service` of log lines.
    service: Optional[str] = Field(None, max_length=100)


@app.get("/health")
def health_check():
    """Liveness probe. Cheap, no external calls."""
    return {
        "status": "ok",
        "database": "postgres" if settings.is_postgres else "sqlite",
        "pgvector": settings.use_pgvector,
        "llm_provider": settings.LLM_PROVIDER,
        "embedding_model": settings.EMBEDDING_MODEL,
        "signup_enabled": settings.SIGNUP_ENABLED,
    }


@app.get("/health/llm")
async def health_check_llm():
    """
    Readiness probe that actually calls the LLM.

    Worth having separately: the API can be perfectly healthy while the model
    endpoint is unreachable, and you want to know which one is broken.
    """
    return await llm.health_check()

# SECTION: Password Hashing Utilities (NEW)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Checks if a plain-text password matches a hashed password.
    """
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """
    Hashes a plain-text password.
    """
    return pwd_context.hash(password)

# SECTION: JWT Token Utilities (NEW)
def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Creates a signed JWT access token.

    - data: dictionary of claims to embed in the token (e.g. {"sub": username, "user_id": 1})
    - expires_delta: optional timedelta for custom expiration; if not provided,
      ACCESS_TOKEN_EXPIRE_MINUTES is used.
    """
    if not isinstance(data, dict):
        raise ValueError("data passed to create_access_token must be a dict")

    # Copy the input data so we don't accidentally mutate the caller's dictionary
    to_encode = data.copy()

    # Compute expiry time
    if expires_delta is not None:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode["exp"] = expire

    # Encode and sign the token using our SECRET_KEY and ALGORITHM
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> dict:
    """
    Decodes a JWT access token and returns its payload (data).
    Raises JWTError if the token is invalid or expired.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


# SECTION: Bearer Token / Current User Dependencies

bearer_scheme = HTTPBearer()


def get_user_by_username(db: Session, username: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.username == username).first()


def get_user_by_id(db: Session, user_id: int) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.id == user_id).first()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: DbSessionDep,
) -> models.User:
    """
    Extracts the current user from the JWT access token.
    Raises 401 if the token is invalid or user does not exist.
    """
    token = credentials.credentials

    try:
        payload = decode_access_token(token)
    except HTTPException:
        # normalize as 401 with proper header
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username: str | None = payload.get("sub")
    user_id: int | None = payload.get("user_id")

    if username is None or user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = get_user_by_id(db, user_id)
    if user is None or user.username != username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_current_admin(
    current_user: Annotated[models.User, Depends(get_current_user)],
) -> models.User:
    """
    Ensures that the current user is an admin.
    Raises 403 if not.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions (admin required)",
        )
    return current_user


# SECTION: Auth Endpoints
@app.post("/auth/signup", status_code=status.HTTP_201_CREATED)
def signup(user_data: UserCreate, db: DbSessionDep, request: Request):
    """
    Create a new user account with its own private workspace.

    Disabled unless SIGNUP_ENABLED=true. It stays off until isolation, rate
    limits and upload protections have been verified. New users are never admins.
    """
    if not settings.SIGNUP_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public signup is currently disabled.",
        )
    limiter.check(f"signup:{client_ip(request)}", limit=5, window_seconds=3600)

    # Check if username or email already exists
    existing_user = (
        db.query(models.User)
        .filter(
            (models.User.username == user_data.username)
            | (models.User.email == user_data.email)
        )
        .first()
    )

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already registered.",
        )

    hashed_password = get_password_hash(user_data.password)

    new_user = models.User(
        username=user_data.username,
        email=user_data.email,
        hashed_password=hashed_password,
        is_admin=False,  # default new users to non-admin
    )

    db.add(new_user)
    db.flush()

    # Every signup gets its own workspace: the isolation boundary.
    workspace = models.Workspace(name=f"{new_user.username}'s workspace")
    db.add(workspace)
    db.flush()
    db.add(models.WorkspaceMember(workspace_id=workspace.id, user_id=new_user.id, role="owner"))
    new_user.default_workspace_id = workspace.id

    db.commit()
    db.refresh(new_user)

    return {
        "id": new_user.id,
        "username": new_user.username,
        "email": new_user.email,
        "is_admin": new_user.is_admin,
        "message": "User created successfully.",
    }

@app.post("/auth/login", response_model=TokenResponse)
def login(form_data: UserLogin, db: DbSessionDep, request: Request):
    """
    Log in a user and return a JWT access token.

    In a more 'OAuth2' style implementation, you'd accept form-encoded data using
    OAuth2PasswordRequestForm, but here we keep it simple with JSON.
    """
    limiter.check(f"login:{client_ip(request)}", limit=10, window_seconds=60)

    user = get_user_by_username(db, form_data.username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
        )

    if not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
        )

    # Create token payload; sub = subject (standard JWT claim)
    token_data = {
        "sub": user.username,
        "user_id": user.id,
        "is_admin": user.is_admin,
    }

    access_token = create_access_token(data=token_data)

    return TokenResponse(access_token=access_token)

# SECTION: Current user + Incident endpoints
@app.get("/me")
def read_current_user(
    current_user: Annotated[models.User, Depends(get_current_user)],
):
    """Who am I? Used by the front end to decide whether to show admin controls."""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "is_admin": current_user.is_admin,
        "workspace_id": current_user.default_workspace_id,
    }


# SECTION: Workspace isolation helpers
def require_workspace(user: models.User) -> int:
    if user.default_workspace_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No workspace assigned.")
    return user.default_workspace_id


def get_incident_for_user(db: Session, incident_id: int, user: models.User) -> models.Incident:
    """
    Fetch an incident the user may see, or 404.

    Other workspaces' incidents return 404, not 403, so ids are not
    discoverable. Platform admins can open any incident.
    """
    incident = db.query(models.Incident).filter(models.Incident.id == incident_id).first()
    if incident is None or (
        not user.is_admin and incident.workspace_id != user.default_workspace_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found.")
    return incident


def visible_documents(db: Session, user: models.User):
    """Documents a user may see: shared ones (no workspace) plus their workspace's."""
    q = db.query(models.Document)
    if user.is_admin:
        return q
    return q.filter(
        or_(
            models.Document.workspace_id.is_(None),
            models.Document.workspace_id == user.default_workspace_id,
        )
    )


@app.post("/incidents", status_code=status.HTTP_201_CREATED)
def create_incident(
    payload: IncidentCreate,
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
):
    """Create an incident. Replaces the old unauthenticated debug endpoint."""
    workspace_id = require_workspace(current_user)
    incident = models.Incident(
        title=payload.title,
        description=payload.description,
        severity=payload.severity,
        status=payload.status or models.IncidentStatus.OPEN,
        creator_id=current_user.id,
        workspace_id=workspace_id,
        service=(payload.service or "").strip() or None,
        started_at=log_matching.as_utc(payload.started_at) or datetime.now(timezone.utc),
        source="manual",
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    return _incident_to_dict(incident)


@app.get("/incidents")
def list_incidents(
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
    limit: int = 50,
):
    q = db.query(models.Incident)
    if not current_user.is_admin:
        q = q.filter(models.Incident.workspace_id == current_user.default_workspace_id)
    incidents = (
        q.order_by(models.Incident.created_at.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {"count": len(incidents), "incidents": [_incident_to_dict(i) for i in incidents]}


@app.get("/incidents/{incident_id}")
def get_incident(
    incident_id: int,
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
):
    return _incident_to_dict(get_incident_for_user(db, incident_id, current_user))


def _incident_to_dict(i: models.Incident) -> dict:
    return {
        "id": i.id,
        "title": i.title,
        "description": i.description,
        "status": i.status,
        "severity": i.severity,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "creator_id": i.creator_id,
        "service": i.service,
        "started_at": i.started_at.isoformat() if i.started_at else None,
        "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
        "source": i.source,
    }


@app.get("/documents")
def list_documents(
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
    type: Optional[str] = None,
):
    """List uploaded documents, optionally filtered by type (log / runbook)."""
    q = visible_documents(db, current_user)
    if type:
        q = q.filter(models.Document.type == type)
    docs = q.order_by(models.Document.uploaded_at.desc()).limit(200).all()

    return {
        "count": len(docs),
        "documents": [
            {
                "id": d.id,
                "title": d.title,
                "type": d.type,
                "status": d.status,
                "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
                "chunk_count": len(d.chunks) if d.type == models.DocumentType.RUNBOOK else None,
            }
            for d in docs
        ],
    }


# SECTION: Debug endpoints (disabled unless ENABLE_DEBUG_ENDPOINTS=true)
if settings.ENABLE_DEBUG_ENDPOINTS:

    @app.post("/debug/create-incident", status_code=status.HTTP_201_CREATED)
    def create_debug_incident(
        db: DbSessionDep,
        current_admin: Annotated[models.User, Depends(get_current_admin)],
    ):
        """Local testing helper. Requires an admin token even when enabled."""
        incident = models.Incident(
            title="Debug Test Incident",
            description="A temporary incident for RAG feature testing.",
            status=models.IncidentStatus.OPEN,
            severity="SEV-3",
            creator_id=current_admin.id,
        )
        db.add(incident)
        db.commit()
        db.refresh(incident)
        return {"message": "Debug incident created.", "incident_id": incident.id}

# helper to detect file type
def detect_document_type(file: UploadFile) -> str:
    """
    File detection based on file extension.
    In a real system, we'd also inspect content signatures (magic numbers).
    """
    filename = file.filename or ""
    lower_name = filename.lower()

    # Common log extensions
    if lower_name.endswith((".log", ".jsonl", ".ndjson")):
        return models.DocumentType.LOG
    if lower_name.endswith(".pdf"):
        return models.DocumentType.RUNBOOK
    if lower_name.endswith(".txt") or lower_name.endswith(".md"):
        return models.DocumentType.RUNBOOK

    return models.DocumentType.OTHER


# SECTION: Embedding Model Initialization
# Global variable to hold the embedding model
embedding_model: Optional["SentenceTransformer"] = None


def get_embedding_model() -> "SentenceTransformer":
    """
    Lazy-load the SentenceTransformer model.
    We only load it once and reuse it to avoid expensive reloads.
    """
    global embedding_model
    if embedding_model is None:
        # Load a small, fast model suitable for CPU and local development.
        # This model is good for general-purpose sentence embeddings.
        from sentence_transformers import SentenceTransformer

        print(f"Loading SentenceTransformer model: {settings.EMBEDDING_MODEL} ...")
        embedding_model = SentenceTransformer(settings.EMBEDDING_MODEL)
        print("Model loaded.")
    return embedding_model


def generate_embedding(text: str) -> list[float]:
    """
    Generates an embedding vector (list of floats) for a given text.
    """
    model = get_embedding_model()
    # Encode the text to get its embedding
    embedding = model.encode(text)
    # Convert numpy array to list for JSON serialization and database storage
    return embedding.tolist()

# SECTION: Cosine similarity helper for vector search
#phase 3 function
def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """
    Computes cosine similarity between two vectors.

    Cosine similarity measures how close two vectors are in direction.
    1.0 -> identical direction (very similar)
    0.0 -> orthogonal (unrelated)
    -1.0 -> opposite direction

    We assume both vectors are non-empty and of the same length.
    """
    if not vec_a or not vec_b:
        return 0.0

    # Compute dot product and magnitudes
    dot = 0.0
    mag_a = 0.0
    mag_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        mag_a += a * a
        mag_b += b * b

    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0

    return dot / ((mag_a ** 0.5) * (mag_b ** 0.5))


# SECTION: Chunking
def chunk_text(
    text: str,
    max_chars: int = None,
    overlap: int = None,
) -> list[str]:
    """
    Split document text into overlapping chunks.

    The old approach split on blank lines and truncated anything over 2000
    characters with an ellipsis, which threw away real content: a long runbook
    section would lose everything past the cutoff, and the answer to a question
    could sit in the discarded tail.

    This version packs paragraphs up to a size limit, hard-splits paragraphs
    that are individually too long, and carries a short overlap between chunks
    so a procedure spanning a boundary is still retrievable from both sides.
    """
    max_chars = max_chars or settings.CHUNK_MAX_CHARS
    overlap = overlap or settings.CHUNK_OVERLAP_CHARS

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    # Break any single paragraph that already exceeds the limit.
    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        start = 0
        while start < len(para):
            pieces.append(para[start : start + max_chars])
            start += max_chars - overlap

    # Pack pieces into chunks.
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif len(current) + len(piece) + 2 <= max_chars:
            current = f"{current}\n\n{piece}"
        else:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{piece}".strip() if tail else piece

    if current:
        chunks.append(current)

    return chunks


def serialize_embedding(vector: list[float]):
    """pgvector takes the list directly; SQLite needs a JSON string."""
    return vector if settings.use_pgvector else json.dumps(vector)


def deserialize_embedding(stored) -> list[float]:
    if stored is None:
        return []
    if isinstance(stored, str):
        try:
            return json.loads(stored)
        except json.JSONDecodeError:
            return []
    return list(stored)


# SECTION: Retrieval
def retrieve_relevant_chunks(
    db: Session,
    query: str,
    top_k: int = None,
    threshold: float = None,
) -> list[dict]:
    """
    Semantic search over runbook chunks.

    On Postgres with pgvector this is a single indexed SQL query: the database
    does the distance maths and returns only top_k rows.

    On SQLite we fall back to loading every chunk and scoring in Python, which
    is what the original code did everywhere. That is O(n) in both rows and
    memory and is the main reason this project needed Postgres.
    """
    top_k = top_k or settings.TOP_K_CHUNKS
    threshold = settings.SIMILARITY_THRESHOLD if threshold is None else threshold

    query_embedding = generate_embedding(query)

    if settings.use_pgvector:
        # cosine_distance = 1 - cosine_similarity
        distance = models.DocumentChunk.embedding.cosine_distance(query_embedding)
        rows = (
            db.query(
                models.DocumentChunk,
                models.Document,
                distance.label("distance"),
            )
            .join(models.Document, models.Document.id == models.DocumentChunk.document_id)
            .filter(
                models.Document.type == models.DocumentType.RUNBOOK,
                models.DocumentChunk.embedding.isnot(None),
            )
            .order_by(distance)
            .limit(top_k)
            .all()
        )
        scored = [
            {
                "similarity": 1.0 - float(dist),
                "chunk_id": chunk.id,
                "document_id": doc.id,
                "document_title": doc.title,
                "chunk_order": chunk.chunk_order,
                "chunk_text": chunk.chunk_text,
            }
            for chunk, doc, dist in rows
        ]
    else:
        chunks_with_docs = (
            db.query(models.DocumentChunk, models.Document)
            .join(models.Document, models.Document.id == models.DocumentChunk.document_id)
            .filter(
                models.Document.type == models.DocumentType.RUNBOOK,
                models.DocumentChunk.embedding.isnot(None),
            )
            .all()
        )
        scored = []
        for chunk, doc in chunks_with_docs:
            vec = deserialize_embedding(chunk.embedding)
            if not vec:
                continue
            scored.append(
                {
                    "similarity": cosine_similarity(query_embedding, vec),
                    "chunk_id": chunk.id,
                    "document_id": doc.id,
                    "document_title": doc.title,
                    "chunk_order": chunk.chunk_order,
                    "chunk_text": chunk.chunk_text,
                }
            )
        scored.sort(key=lambda item: item["similarity"], reverse=True)
        scored = scored[:top_k]

    return [s for s in scored if s["similarity"] >= threshold]



def build_log_context(log_patterns: list[dict]) -> str:
    """Format related historical log patterns for the LLM prompt."""
    if not log_patterns:
        return "No related historical log patterns were found."

    sections = []
    for index, pattern in enumerate(log_patterns, start=1):
        sections.append(
            f"[Historical Log Pattern {index}]\n"
            f"Source file: {pattern['document_title']}\n"
            f"Level: {pattern['level']}\n"
            f"Message: {pattern['message']}\n"
            f"Occurrences: {pattern['occurrence_count']}\n"
            f"Most recent occurrence: "
            f"{pattern['latest_timestamp'] or 'unknown'}"
        )

    return "\n\n".join(sections)

# SECTION: Log ingestion (uploaded files -> structured log_entries)
LOG_EXTENSIONS = (".log", ".txt", ".json", ".jsonl", ".ndjson")


async def _read_upload_limited(file: UploadFile) -> bytes:
    """Read an upload into memory, refusing anything over MAX_UPLOAD_MB."""
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    buffer = bytearray()
    while chunk := await file.read(1024 * 1024):
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum is {settings.MAX_UPLOAD_MB} MB.",
            )
    return bytes(buffer)


def _ingest_logs(
    db: Session,
    data: bytes,
    title: str,
    uploader: models.User,
    workspace_id: Optional[int],
    incident: Optional[models.Incident],
    default_service: Optional[str],
) -> dict:
    """
    Parse uploaded log bytes into LogEntry rows.

    The raw file is NOT kept on disk (hosted disks are ephemeral and storage is
    tight); the parsed rows are the source of truth. Enforces the free-tier
    budget: upload size, line count, per-workspace row cap and total DB size.
    """
    if b"\x00" in data[:8192]:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="This looks like a binary file, not a text log.",
        )

    digest = hashlib.sha256(data).hexdigest()
    ws_filter = (
        models.Document.workspace_id.is_(None)
        if workspace_id is None
        else models.Document.workspace_id == workspace_id
    )
    duplicate = (
        db.query(models.Document)
        .filter(models.Document.type == models.DocumentType.LOG,
                models.Document.sha256 == digest, ws_filter)
        .first()
    )
    if duplicate:
        return {
            "id": duplicate.id, "title": duplicate.title, "type": duplicate.type,
            "status": duplicate.status, "duplicate": True, "log_entries_created": 0,
            "message": "This exact file was already uploaded.",
        }

    size_mb = database_size_mb()
    if size_mb is not None and size_mb >= settings.DB_STORAGE_LIMIT_MB:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail="Storage limit reached. Ask an admin to purge old logs.",
        )

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if text.lstrip().startswith("["):  # a JSON array of log objects
        try:
            parsed_array = json.loads(text)
            if isinstance(parsed_array, list):
                lines = [json.dumps(o) for o in parsed_array if isinstance(o, dict)]
        except ValueError:
            pass

    if len(lines) > settings.MAX_LINES_PER_UPLOAD:
        raise HTTPException(
            status_code=413,
            detail=f"Too many lines ({len(lines)}). Maximum per upload is {settings.MAX_LINES_PER_UPLOAD}.",
        )
    if workspace_id is not None:
        existing_rows = (
            db.query(func.count(models.LogEntry.id))
            .filter(models.LogEntry.workspace_id == workspace_id)
            .scalar()
            or 0
        )
        if existing_rows + len(lines) > settings.MAX_LOG_ENTRIES_PER_WORKSPACE:
            raise HTTPException(
                status_code=413,
                detail="Workspace log quota exceeded. Old logs are purged automatically after "
                       f"{settings.LOG_RETENTION_DAYS} days.",
            )

    document = models.Document(
        title=title,
        file_path="(not retained)",
        type=models.DocumentType.LOG,
        status="processing",
        uploader_id=uploader.id,
        workspace_id=workspace_id,
        incident_id=incident.id if incident else None,
        size_bytes=len(data),
        sha256=digest,
    )
    db.add(document)
    db.flush()

    created = with_timestamp = 0
    services: dict[str, int] = {}
    batch: list[dict] = []
    try:
        for parsed in log_parsing.parse_log_lines(lines, default_service):
            raw = parsed["raw"]
            # Keep the raw line only where it adds information (JSON / unparsed).
            keep_raw = parsed["timestamp"] is None or raw.startswith("{")
            batch.append(
                {
                    "document_id": document.id,
                    "incident_id": incident.id if incident else None,
                    "workspace_id": workspace_id,
                    "timestamp": parsed["timestamp"],
                    "level": parsed["level"],
                    "message": parsed["message"],
                    "raw": raw[:2000] if keep_raw else None,
                    "service": parsed["service"],
                    "trace_id": parsed["trace_id"],
                    "host": parsed["host"],
                    "attributes": parsed["attributes"],
                }
            )
            created += 1
            with_timestamp += 1 if parsed["timestamp"] else 0
            if parsed["service"]:
                services[parsed["service"]] = services.get(parsed["service"], 0) + 1
            if len(batch) >= 1000:
                db.execute(insert(models.LogEntry), batch)
                batch = []
        if batch:
            db.execute(insert(models.LogEntry), batch)
        if created == 0:
            raise ValueError("no log lines found")
        document.status = "processed"
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[ingest] failed: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=422,
            detail="Could not parse any log lines from this file.",
        )

    return {
        "id": document.id,
        "title": document.title,
        "type": document.type,
        "status": document.status,
        "duplicate": False,
        "log_entries_created": created,
        "entries_with_timestamp": with_timestamp,
        "services_detected": sorted(services, key=services.get, reverse=True)[:20],
    }


@app.post("/incidents/{incident_id}/logs", status_code=status.HTTP_201_CREATED)
async def upload_incident_logs(
    incident_id: int,
    file: Annotated[UploadFile, File(...)],
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
    title: Annotated[Optional[str], Form()] = None,
    service: Annotated[Optional[str], Form()] = None,
):
    """
    Upload a log file (.log, .txt, .json, .jsonl, .ndjson) for an incident.

    Any signed-in user may upload to incidents in their own workspace. The
    client's filename is only used as a display title; it never touches the
    filesystem.
    """
    incident = get_incident_for_user(db, incident_id, current_user)
    limiter.check(f"upload:{current_user.id}", limit=20, window_seconds=3600)

    filename = os.path.basename(file.filename or "")
    if not filename or not filename.lower().endswith(LOG_EXTENSIONS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Upload .log, .txt, .json, .jsonl or .ndjson.",
        )

    data = await _read_upload_limited(file)
    result = await asyncio.to_thread(
        _ingest_logs,
        db,
        data,
        (title or filename)[:200],
        current_user,
        incident.workspace_id,
        incident,
        (service or incident.service or "").strip() or None,
    )
    return result


@app.get("/incidents/{incident_id}/logs")
async def match_incident_logs(
    incident_id: int,
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
    before_minutes: Optional[int] = None,
    after_minutes: Optional[int] = None,
    service: Optional[str] = None,
    limit: int = 20,
):
    """
    Rank log clusters that may be connected to this incident, with reasons.

    Deterministic: time window around the incident start, service match, term
    overlap with the incident text, level and frequency. No LLM involved.
    """
    incident = get_incident_for_user(db, incident_id, current_user)
    services = [s.strip() for s in (service or "").split(",") if s.strip()] or (
        [incident.service] if incident.service else []
    )
    result = await asyncio.to_thread(
        log_matching.match_logs,
        DatabaseLogSource(db),
        workspace_id=incident.workspace_id,
        title=incident.title,
        description=incident.description,
        services=services,
        anchor=incident.started_at,
        resolved_at=incident.resolved_at,
        before_minutes=settings.LOG_WINDOW_BEFORE_MINUTES if before_minutes is None else max(0, min(before_minutes, 1440)),
        after_minutes=settings.LOG_WINDOW_AFTER_MINUTES if after_minutes is None else max(0, min(after_minutes, 1440)),
        max_rows=settings.MATCH_MAX_ROWS,
        top_n=max(1, min(limit, 50)),
    )
    return {"incident_id": incident.id, **result}


@app.get("/incidents/{incident_id}/logs/clusters/{cluster_id}/entries")
async def cluster_log_entries(
    incident_id: int,
    cluster_id: str,
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
    before_minutes: Optional[int] = None,
    after_minutes: Optional[int] = None,
    service: Optional[str] = None,
):
    """All raw lines in one matched cluster (the drill-down view)."""
    incident = get_incident_for_user(db, incident_id, current_user)
    services = [s.strip() for s in (service or "").split(",") if s.strip()] or (
        [incident.service] if incident.service else []
    )
    entries = await asyncio.to_thread(
        log_matching.cluster_entries,
        DatabaseLogSource(db),
        cluster_id,
        workspace_id=incident.workspace_id,
        title=incident.title,
        description=incident.description,
        services=services,
        anchor=incident.started_at,
        resolved_at=incident.resolved_at,
        before_minutes=settings.LOG_WINDOW_BEFORE_MINUTES if before_minutes is None else max(0, min(before_minutes, 1440)),
        after_minutes=settings.LOG_WINDOW_AFTER_MINUTES if after_minutes is None else max(0, min(after_minutes, 1440)),
        max_rows=settings.MATCH_MAX_ROWS,
    )
    return {"incident_id": incident.id, "cluster_id": cluster_id, "count": len(entries), "entries": entries}


@app.post("/admin/upload", status_code=status.HTTP_201_CREATED)
@app.post("/documents/upload", status_code=status.HTTP_201_CREATED)
async def upload_document(
    title: Annotated[str, Form(...)] ,
    file: Annotated[UploadFile, File(...)] ,
    db: DbSessionDep,
    current_admin: Annotated[models.User, Depends(get_current_admin)],
    ):
    """
    Admin endpoint to upload a shared file.

    - Log files (.log/.jsonl/.ndjson): parsed into shared LogEntry records.
    - Runbooks/docs (pdf/txt/md): parsed, chunked and embedded.
    Regular users upload logs per incident via POST /incidents/{id}/logs.
    """

    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must have a name.",
        )

    doc_type = detect_document_type(file)

    # For now, only allow log and runbook/doc types
    if doc_type not in (models.DocumentType.LOG, models.DocumentType.RUNBOOK):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Please upload .log, .jsonl, .ndjson, .pdf, .txt, or .md files.",
        )

    if doc_type == models.DocumentType.LOG:
        data = await _read_upload_limited(file)
        return await asyncio.to_thread(
            _ingest_logs, db, data, title[:200], current_admin, None, None, None
        )

    # Generating safe unique filename
    file_extension = os.path.splitext(file.filename)[1] or ""
    unique_id = str(uuid.uuid4())
    safe_filename = f"{unique_id}{file_extension}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    # Save to disk
    try:
        with open(file_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save file: {e}",
        )

    # Create Document record with initial status
    document = models.Document(
        title=title,
        file_path=file_path,
        type=doc_type,
        status="processing",
        uploader_id=current_admin.id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    # If it's a runbook/doc, parse text, chunk, and embed
    if doc_type == models.DocumentType.RUNBOOK:
        full_text = ""
        try:
            # Check file extension before trying pypdf
            if file_extension.lower() == ".pdf":
                reader = PdfReader(file_path)
                for page_num, page in enumerate(reader.pages):
                    extracted = page.extract_text() or ""
                    full_text += extracted + "\n"
            else:  # For .txt, .md, etc., just read the raw file
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    full_text = f.read()

        except Exception as e:
            document.status = "failed"
            db.add(document)
            db.commit()
            os.remove(file_path)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to parse document content: {e}",
            )

        # Overlapping chunking. See chunk_text() for why this replaced the
        # old split-on-blank-line-and-truncate approach.
        raw_chunks = chunk_text(full_text)

        if not raw_chunks and full_text.strip():
            raw_chunks = [full_text.strip()]
        elif not raw_chunks:
            document.status = "failed"
            db.add(document)
            db.commit()
            os.remove(file_path)
            raise HTTPException(
                status_code=422,
                detail="Document contains no extractable text content.",
            )

        document_chunks = []
        for i, text_piece in enumerate(raw_chunks):
            chunk_embedding = generate_embedding(text_piece)

            db_chunk = models.DocumentChunk(
                document_id=document.id,
                chunk_text=text_piece,
                chunk_order=i,
                embedding=serialize_embedding(chunk_embedding),
                embedding_model=settings.EMBEDDING_MODEL,
            )
            document_chunks.append(db_chunk)

        db.add_all(document_chunks)
        document.status = "processed"
        db.add(document)
        db.commit()
        db.refresh(document)

        return {
            "id": document.id,
            "title": document.title,
            "type": document.type,
            "status": document.status,
            "chunk_count": len(document_chunks),
            "message": "Runbook/document uploaded, parsed, chunked, and embeddings generated.",
        }

    # Should not happen due to prior type check, but here for safety
    else:
        document.status = "failed"
        db.add(document)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected document type encountered.",
        )


@app.get("/logs/{document_id}/summary", response_model=dict)
def get_log_summary(
    document_id: int,
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
):
    """
    Retrieves a summary of log entries for a given document.

    Includes:
    - Total log entries
    - Count by log level (INFO, WARN, ERROR, etc.)
    - Clustered common messages (top N most frequent messages)
    - Time range of logs
    """
    # Verifying document exists and is a log file
    document = visible_documents(db, current_user).filter(models.Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    if document.type != models.DocumentType.LOG:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Document is not a log file.")

    # 1. Basic count
    total_entries = db.query(models.LogEntry).filter(models.LogEntry.document_id == document_id).count()

    # 2. Get min/max timestamps for the log entries
    time_range = (
        db.query(
            func.min(models.LogEntry.timestamp),
            func.max(models.LogEntry.timestamp),
        )
        .filter(models.LogEntry.document_id == document_id)
        .first()
    )
    first_log_time, last_log_time = time_range if time_range else (None, None)

    # 3. Count by log level
    level_counts = (
        db.query(models.LogEntry.level, func.count(models.LogEntry.id))
        .filter(models.LogEntry.document_id == document_id)
        .group_by(models.LogEntry.level)
        .order_by(func.count(models.LogEntry.id).desc())
        .all()
    )

    # Format: {"INFO": 100, "ERROR": 20, "WARN": 5}
    formatted_level_counts = {level: count for level, count in level_counts if level is not None}
    # Include entries without a recognized level
    if None in [lc[0] for lc in level_counts]:
        formatted_level_counts["UNKNOWN_LEVEL"] = next(
            (lc[1] for lc in level_counts if lc[0] is None), 0
        )

    # 4. Cluster common messages (top N)
    message_clusters = (
        db.query(models.LogEntry.message, func.count(models.LogEntry.id))
        .filter(models.LogEntry.document_id == document_id)
        .group_by(models.LogEntry.message)
        .order_by(func.count(models.LogEntry.id).desc())
        .limit(10)  # Get top 10 most frequent messages
        .all()
    )
    # Format: [{"message": "Database timeout", "count": 150}, ...]
    formatted_message_clusters = [{"message": msg, "count": count} for msg, count in message_clusters]

    return {
        "document_id": document_id,
        "total_log_entries": total_entries,
        "first_log_entry_at": first_log_time.isoformat() if first_log_time else None,
        "last_log_entry_at": last_log_time.isoformat() if last_log_time else None,
        "level_counts": formatted_level_counts,
        "top_message_clusters": formatted_message_clusters,
    }

#phase 3
@app.get("/search/runbooks", response_model=dict)
def search_runbooks(
    query: str,
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
    top_k: int = 5,
    threshold: float = 0.0,
):
    """
    Semantic search over runbook chunks.

    Retrieval now lives in retrieve_relevant_chunks() so that this endpoint and
    the RAG endpoint cannot drift apart. Default threshold is 0.0 here because
    when you are inspecting search quality you want to see the weak matches too.
    """
    results = retrieve_relevant_chunks(db, query, top_k=top_k, threshold=threshold)

    return {
        "query": query,
        "top_k": top_k,
        "threshold": threshold,
        "backend": "pgvector" if settings.use_pgvector else "python",
        "result_count": len(results),
        "results": [
            {**r, "similarity": round(float(r["similarity"]), 4)} for r in results
        ],
    }


# SECTION: RAG Endpoint for Incident Investigation
@app.post("/incidents/{incident_id}/ask", response_model=dict)
async def ask_incident_assistant(
    incident_id: int,
    question: Annotated[str, Form(...)],
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
    top_k_chunks: int = None,
):
    """
    Ask a question about an incident, answered from the runbook corpus.

    Flow: retrieve -> build a numbered context block -> call the configured LLM
    -> persist the question and answer for analytics.
    """
    incident = get_incident_for_user(db, incident_id, current_user)
    limiter.check(f"ask:{current_user.id}", limit=30, window_seconds=3600)

    question = question.strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Question cannot be empty.",
        )

    top_k = top_k_chunks or settings.TOP_K_CHUNKS

    # Retrieval happens off the event loop: embedding a query is CPU-bound and
    # would otherwise block every other request on this worker.
    relevant_chunks = await asyncio.to_thread(
        retrieve_relevant_chunks, db, question, top_k
    )

    # Matching uploaded logs to this incident (deterministic, see log_matching.py).
    log_match = await asyncio.to_thread(
        log_matching.match_logs,
        DatabaseLogSource(db),
        workspace_id=incident.workspace_id,
        title=incident.title,
        description=incident.description,
        services=[incident.service] if incident.service else [],
        anchor=incident.started_at,
        resolved_at=incident.resolved_at,
        extra_text=question,
        before_minutes=settings.LOG_WINDOW_BEFORE_MINUTES,
        after_minutes=settings.LOG_WINDOW_AFTER_MINUTES,
        max_rows=settings.MATCH_MAX_ROWS,
        top_n=5,
    )
    relevant_log_patterns = log_match["clusters"]
    log_context = build_log_context(relevant_log_patterns)

    if relevant_chunks:
        context_block, citations = llm.build_context_block(relevant_chunks)
        system_prompt = llm.GROUNDED_SYSTEM_PROMPT
        user_prompt = (
            f"Incident: {incident.title}\n"
            f"Incident description: {incident.description or 'not provided'}\n\n"
            f"Question: {question}\n\n"
            f"Official runbook context:\n{context_block}\n\n"
            f"Related historical log patterns:\n{log_context}\n\n"
            "Use the runbook as the primary troubleshooting source. "
            "Use historical logs only as supporting evidence about patterns "
            "that occurred before. Do not claim a historical pattern is the "
            "confirmed cause of the current incident."
        )
        grounded = True
        message = (
            "Answer generated from runbook context and related historical "
            "log patterns."
            if relevant_log_patterns
            else "Answer generated from runbook context."
        )
    else:
        context_block, citations = "", []
        system_prompt = llm.UNGROUNDED_SYSTEM_PROMPT
        user_prompt = (
            f"Incident: {incident.title}\n"
            f"Incident description: {incident.description or 'not provided'}\n\n"
            f"Question: {question}\n\n"
            "No relevant runbook guidance was retrieved.\n\n"
            f"Related historical log patterns:\n{log_context}\n\n"
            "Historical logs are supporting evidence only. Explain that no "
            "official runbook guidance was found, and do not present a "
            "historical pattern as the confirmed cause of the current incident."
        )
        grounded = False
        message = (
            "No runbook content cleared the similarity threshold. "
            "Related historical log patterns were included as supporting evidence."
            if relevant_log_patterns
            else (
                "No runbook content or related historical log patterns were found. "
                "Answer is general guidance only."
            )
        )

    result = await llm.generate(system_prompt, user_prompt)

    # Persist once, after we have the answer. The old code committed the
    # question first and then committed again with the answer, doubling the
    # writes on every single request.
    question_log = models.QuestionLog(
        incident_id=incident_id,
        user_id=current_user.id,
        question=question,
        ai_answer=result.text,
    )
    db.add(question_log)
    db.commit()
    db.refresh(question_log)

    return {
        "incident_id": incident_id,
        "question_log_id": question_log.id,
        "question": question,
        "answer": result.text,
        "citations": citations,
        "grounded": grounded,
        "llm": {
            "provider": result.provider,
            "model": result.model,
            "degraded": result.degraded,
            "error": result.error,
        },
        "retrieval": {
            "backend": "pgvector" if settings.use_pgvector else "python",
            "threshold": settings.SIMILARITY_THRESHOLD,
            "chunks_used": len(citations),
            "historical_log_patterns_used": len(relevant_log_patterns),
        },
        "historical_log_patterns": relevant_log_patterns,
        "message": message,
    }


@app.post("/feedback", status_code=status.HTTP_201_CREATED)
def submit_answer_feedback(
    feedback_data: AnswerFeedbackSubmit,
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
):
    """
    Allows users to submit feedback on an AI-generated answer,
    linking it directly to a specific question log entry.
    """
    # 1. Retrieve the original question log entry
    question_log_entry = db.query(models.QuestionLog).filter(
        models.QuestionLog.id == feedback_data.question_log_id
    ).first()

    if question_log_entry is not None and not current_user.is_admin:
        owner_incident = db.query(models.Incident).filter(
            models.Incident.id == question_log_entry.incident_id
        ).first()
        if owner_incident is None or owner_incident.workspace_id != current_user.default_workspace_id:
            question_log_entry = None

    if not question_log_entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Question log entry with ID {feedback_data.question_log_id} not found."
        )
    
    # 2. Create feedback record
    new_feedback = models.AnswerFeedback(
        incident_id=question_log_entry.incident_id, # Link to the incident from the log
        user_id=current_user.id, # The user submitting feedback
        question=question_log_entry.question, # The original question
        answer=question_log_entry.ai_answer, # The AI's full answer from the log
        is_helpful=feedback_data.is_helpful,
        feedback_text=feedback_data.feedback_text,
    )

    db.add(new_feedback)
    db.commit()
    db.refresh(new_feedback)

    return {
        "feedback_id": new_feedback.id,
        "question_log_id": question_log_entry.id,
        "message": "Feedback submitted successfully.",
        "is_helpful": new_feedback.is_helpful,
    }


# SECTION: Analytics
@app.get("/analytics/summary")
def analytics_summary(
    db: DbSessionDep,
    current_user: Annotated[models.User, Depends(get_current_user)],
):
    """
    Aggregate view of how the assistant is being used and whether it is helping.

    The feedback loop only has value if you can see it, so this surfaces the
    helpful/unhelpful split alongside volume.
    """
    ws = current_user.default_workspace_id
    is_admin = bool(current_user.is_admin)

    def in_workspace(query, model):
        """Non-admins only see activity on incidents in their own workspace."""
        if is_admin:
            return query
        return query.join(models.Incident, models.Incident.id == model.incident_id).filter(
            models.Incident.workspace_id == ws
        )

    total_questions = in_workspace(
        db.query(func.count(models.QuestionLog.id)), models.QuestionLog
    ).scalar() or 0
    total_feedback = in_workspace(
        db.query(func.count(models.AnswerFeedback.id)), models.AnswerFeedback
    ).scalar() or 0
    helpful = (
        in_workspace(db.query(func.count(models.AnswerFeedback.id)), models.AnswerFeedback)
        .filter(models.AnswerFeedback.is_helpful.is_(True))
        .scalar()
        or 0
    )

    recent = (
        in_workspace(db.query(models.QuestionLog), models.QuestionLog)
        .order_by(models.QuestionLog.answered_at.desc())
        .limit(10)
        .all()
    )

    visible_doc_ids = visible_documents(db, current_user).with_entities(models.Document.id)
    log_entry_query = db.query(func.count(models.LogEntry.id))
    if not is_admin:
        log_entry_query = log_entry_query.filter(
            or_(models.LogEntry.workspace_id == ws, models.LogEntry.workspace_id.is_(None))
        )

    return {
        "total_questions": total_questions,
        "total_feedback": total_feedback,
        "helpful_count": helpful,
        "unhelpful_count": total_feedback - helpful,
        "helpful_rate": round(helpful / total_feedback, 3) if total_feedback else None,
        "documents": {
            "runbooks": visible_documents(db, current_user)
            .filter(models.Document.type == models.DocumentType.RUNBOOK)
            .count(),
            "logs": visible_documents(db, current_user)
            .filter(models.Document.type == models.DocumentType.LOG)
            .count(),
        },
        "total_chunks": db.query(func.count(models.DocumentChunk.id))
        .filter(models.DocumentChunk.document_id.in_(visible_doc_ids))
        .scalar()
        or 0,
        "total_log_entries": log_entry_query.scalar() or 0,
        "recent_questions": [
            {
                "id": q.id,
                "incident_id": q.incident_id,
                "question": q.question,
                "answered_at": q.answered_at.isoformat() if q.answered_at else None,
            }
            for q in recent
        ],
    }


# SECTION: Admin: storage budget and retention
@app.get("/admin/storage")
def storage_status(
    db: DbSessionDep,
    current_admin: Annotated[models.User, Depends(get_current_admin)],
):
    """Where we are against the free-tier budget. Admin only."""
    size_mb = database_size_mb()
    return {
        "database_size_mb": size_mb,
        "database_limit_mb": settings.DB_STORAGE_LIMIT_MB,
        "used_fraction": round(size_mb / settings.DB_STORAGE_LIMIT_MB, 3) if size_mb is not None else None,
        "log_entries": db.query(func.count(models.LogEntry.id)).scalar() or 0,
        "max_log_entries_per_workspace": settings.MAX_LOG_ENTRIES_PER_WORKSPACE,
        "max_upload_mb": settings.MAX_UPLOAD_MB,
        "retention_days": settings.LOG_RETENTION_DAYS,
    }


@app.post("/admin/maintenance/purge")
async def purge_logs_now(
    current_admin: Annotated[models.User, Depends(get_current_admin)],
):
    """Run the retention purge immediately instead of waiting for the timer."""
    return await asyncio.to_thread(retention.purge_old_logs)


# SECTION: Front end
# Mounted last so it does not shadow the API routes above. html=True makes
# StaticFiles serve index.html at "/".
_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "frontend",
    "dist",
)
if os.path.isdir(_STATIC_DIR):
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")