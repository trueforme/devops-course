import os
from contextlib import asynccontextmanager

import psycopg
import redis
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

# Счётчик посещений. Состояние эфемерное: потеря допустима, см. REPORT.md.
cache = redis.Redis(
    host=os.environ.get("REDIS_HOST", "cache"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    decode_responses=True,
)


def database_connection():
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "db"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def create_table():
    with database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                text text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # БД готова: app стартует только после healthcheck db (depends_on).
    create_table()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=PlainTextResponse)
def index() -> str:
    visits = cache.incr("visits")
    return f"Docker notes app - visits: {visits}\n"


@app.get("/notes", response_class=PlainTextResponse)
def list_notes() -> str:
    with database_connection() as connection:
        rows = connection.execute("SELECT id, text FROM notes ORDER BY id").fetchall()
    body = "\n".join(f"{note_id}: {text}" for note_id, text in rows)
    return (body or "(no notes)") + "\n"


@app.get("/notes/add", response_class=PlainTextResponse)
def add_note(text: str = "") -> PlainTextResponse:
    text = text.strip()
    if not text:
        return PlainTextResponse("text is required\n", status_code=400)
    with database_connection() as connection:
        connection.execute("INSERT INTO notes (text) VALUES (%s)", (text,))
    return PlainTextResponse(f"added: {text}\n", status_code=201)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok\n"
