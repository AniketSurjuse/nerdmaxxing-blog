import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any, Iterator
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AnyHttpUrl, BaseModel, Field
from starlette.concurrency import run_in_threadpool

load_dotenv()

if os.name == "nt":
    import truststore

    truststore.inject_into_ssl()

logger = logging.getLogger(__name__)


class BlogCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=200)
    summary: str = Field(default="", max_length=500)
    content: str = Field(
        min_length=1,
        max_length=1_000_000,
        description="Markdown content; inline images use ![alt text](public-image-url) at the desired position.",
    )
    topic: str = Field(min_length=1, max_length=100)
    media_urls: list[AnyHttpUrl] = Field(default_factory=list)


class Blog(BaseModel):
    slug: str
    title: str
    summary: str
    content: str = Field(
        description="Markdown content with inline images preserved at their authored positions."
    )
    topic: str
    media_urls: list[str]
    created_at: str
    updated_at: str


class BlogList(BaseModel):
    items: list[Blog]
    total: int
    limit: int
    offset: int


class ImageUpload(BaseModel):
    url: AnyHttpUrl
    key: str


MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {
    "image/avif": "avif",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def create_r2_client(settings: dict[str, str]) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=settings["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=settings["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 2, "mode": "standard"}),
    )


def detect_image_type(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[4:8] == b"ftyp" and header[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


def create_app(
    database_path: str | Path | None = None,
    admin_token: str | None = None,
    r2_config: dict[str, str] | None = None,
) -> FastAPI:
    configured_database = database_path or os.getenv("DATABASE_PATH", "./data/blogs.db")
    configured_token = admin_token if admin_token is not None else os.getenv("ADMIN_API_TOKEN", "")
    configured_r2 = r2_config if r2_config is not None else {
        name: os.getenv(name, "")
        for name in (
            "R2_ACCOUNT_ID",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET_NAME",
            "R2_PUBLIC_BASE_URL",
        )
    }
    bearer = HTTPBearer(auto_error=False)
    r2_client: Any | None = None

    @contextmanager
    def connect_db() -> Iterator[sqlite3.Connection]:
        path = Path(configured_database)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        with connect_db() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS blogs (
                    slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content TEXT NOT NULL,
                    topic TEXT NOT NULL COLLATE NOCASE,
                    media_urls TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS blogs_topic_idx ON blogs(topic)"
            )
        yield

    api = FastAPI(title="Challenge Resources API", lifespan=lifespan)

    @api.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(Path(__file__).with_name("index.html"))

    def require_admin(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        token = configured_token
        if not token:
            raise HTTPException(status_code=503, detail="Admin API is not configured")
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not secrets.compare_digest(credentials.credentials, token)
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing admin token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def serialize_blog(row: sqlite3.Row) -> Blog:
        return Blog(
            slug=row["slug"],
            title=row["title"],
            summary=row["summary"],
            content=row["content"],
            topic=row["topic"],
            media_urls=json.loads(row["media_urls"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_r2_client() -> Any:
        nonlocal r2_client
        if r2_client is None:
            r2_client = create_r2_client(configured_r2)
        return r2_client

    @api.put("/media", response_model=ImageUpload, status_code=201, dependencies=[Depends(require_admin)])
    async def upload_image(request: Request) -> ImageUpload:
        required_settings = (
            "R2_ACCOUNT_ID",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET_NAME",
            "R2_PUBLIC_BASE_URL",
        )
        if any(not configured_r2.get(name) for name in required_settings):
            raise HTTPException(status_code=503, detail="R2 image storage is not configured")
        public_base_url = configured_r2["R2_PUBLIC_BASE_URL"].rstrip("/")
        if not public_base_url.startswith("https://"):
            raise HTTPException(status_code=503, detail="R2_PUBLIC_BASE_URL must use HTTPS")

        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in IMAGE_TYPES:
            raise HTTPException(status_code=415, detail="Upload a PNG, JPEG, GIF, WebP, or AVIF image")
        try:
            declared_size = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid content length") from None
        if declared_size > MAX_IMAGE_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="Image must be 10 MB or smaller")

        size = 0
        header = bytearray()
        with SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as image_file:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_IMAGE_SIZE_BYTES:
                    raise HTTPException(status_code=413, detail="Image must be 10 MB or smaller")
                if len(header) < 16:
                    header.extend(chunk[: 16 - len(header)])
                image_file.write(chunk)

            detected_type = detect_image_type(bytes(header))
            if not size or detected_type != content_type:
                raise HTTPException(status_code=415, detail="File contents do not match a supported image type")

            key = f"images/{uuid4().hex}.{IMAGE_TYPES[detected_type]}"
            image_file.seek(0)
            try:
                await run_in_threadpool(
                    get_r2_client().put_object,
                    Bucket=configured_r2["R2_BUCKET_NAME"],
                    Key=key,
                    Body=image_file,
                    ContentLength=size,
                    ContentType=detected_type,
                    CacheControl="public, max-age=31536000, immutable",
                )
            except Exception as error:
                logger.exception("R2 image upload failed")
                raise HTTPException(status_code=502, detail="Image upload to R2 failed") from error

        return ImageUpload(url=f"{public_base_url}/{key}", key=key)

    @api.post("/blogs", response_model=Blog, status_code=201, dependencies=[Depends(require_admin)])
    def create_blog(blog: BlogCreate) -> Blog:
        slug_source = blog.slug or blog.title
        slug = re.sub(r"[^a-z0-9]+", "-", slug_source.lower()).strip("-")
        if not slug:
            raise HTTPException(status_code=422, detail="Title or slug must contain letters or numbers")

        now = datetime.now(timezone.utc).isoformat()
        try:
            with connect_db() as connection:
                connection.execute(
                    """
                    INSERT INTO blogs (slug, title, summary, content, topic, media_urls, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        blog.title,
                        blog.summary,
                        blog.content,
                        blog.topic,
                        json.dumps([str(url) for url in blog.media_urls]),
                        now,
                        now,
                    ),
                )
                row = connection.execute("SELECT * FROM blogs WHERE slug = ?", (slug,)).fetchone()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="A blog with this slug already exists") from None
        return serialize_blog(row)

    @api.get("/blogs/search", response_model=BlogList)
    def search_blogs(
        q: str | None = Query(default=None, min_length=1, max_length=100),
        topic: str | None = Query(default=None, min_length=1, max_length=100),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> BlogList:
        search_term = q.strip() if q else ""
        if not search_term and not topic:
            raise HTTPException(status_code=422, detail="Provide a search term or topic")

        conditions = []
        values: list[str] = []
        like_pattern = ""
        if search_term:
            escaped_term = search_term.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            like_pattern = f"%{escaped_term}%"
            conditions.append(
                "(title LIKE ? COLLATE NOCASE ESCAPE '!' OR content LIKE ? COLLATE NOCASE ESCAPE '!')"
            )
            values.extend((like_pattern, like_pattern))
        if topic:
            conditions.append("topic = ? COLLATE NOCASE")
            values.append(topic)

        where_clause = " AND ".join(conditions)
        order_clause = ""
        order_values: list[str] = []
        if search_term:
            order_clause = "ORDER BY CASE WHEN title LIKE ? COLLATE NOCASE ESCAPE '!' THEN 0 ELSE 1 END, created_at DESC"
            order_values.append(like_pattern)
        else:
            order_clause = "ORDER BY created_at DESC"

        with connect_db() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM blogs WHERE {where_clause}", values
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM blogs WHERE {where_clause} {order_clause} LIMIT ? OFFSET ?",
                [*values, *order_values, limit, offset],
            ).fetchall()
        return BlogList(items=[serialize_blog(row) for row in rows], total=total, limit=limit, offset=offset)

    @api.get("/blogs", response_model=BlogList)
    def list_blogs(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> BlogList:
        with connect_db() as connection:
            total = connection.execute("SELECT COUNT(*) FROM blogs").fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM blogs ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return BlogList(items=[serialize_blog(row) for row in rows], total=total, limit=limit, offset=offset)

    @api.get("/blogs/{slug}", response_model=Blog)
    def get_blog(slug: str) -> Blog:
        with connect_db() as connection:
            row = connection.execute("SELECT * FROM blogs WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Blog not found")
        return serialize_blog(row)

    @api.delete("/blogs/{slug}", status_code=204, dependencies=[Depends(require_admin)])
    def delete_blog(slug: str) -> Response:
        with connect_db() as connection:
            result = connection.execute("DELETE FROM blogs WHERE slug = ?", (slug,))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Blog not found")
        return Response(status_code=204)

    return api


app = create_app()