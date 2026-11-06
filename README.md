# Challenge Resources API

A small FastAPI service for public blog reading and token-protected admin mutations. Blog text and media URLs are stored in SQLite; uploaded images are stored in Cloudflare R2.

The root URL (`/`) serves a lightweight browser interface for reading/searching blogs and opening the admin composer. The composer sends the admin bearer token with blog creation and image upload requests. Keep the token private; the browser does not persist it.

## Run

Use Python 3.10 or newer:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

Set a long random `ADMIN_API_TOKEN` in `.env`, then start the single-worker service:

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

The database defaults to `./data/blogs.db`. Keep that directory on persistent local storage and back it up; do not put SQLite on an ephemeral deployment path. The ignored `.env` file must contain `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, and `R2_PUBLIC_BASE_URL`. Set the public base URL to the HTTPS custom domain or public R2 domain configured for the bucket; the endpoint returns image URLs under this domain for Markdown. R2 credentials stay on the server and are never sent to the browser.

## Endpoints

- `POST /blogs` creates a blog; requires `Authorization: Bearer <ADMIN_API_TOKEN>`.
- `PUT /media` uploads one image to R2; requires the same admin bearer token and a raw image body with its `Content-Type` header. Supports PNG, JPEG, GIF, WebP, or AVIF up to 10 MB. Returns the public URL and object key.
- `GET /blogs/{slug}` reads one blog.
- `GET /blogs/search?topic=Python` finds blogs by exact topic, case-insensitively.
- `GET /blogs/search?q=photosynthesis` searches titles and content, ranking title matches first; `?topic=Python` remains available for exact topic search.
- `GET /blogs?limit=20&offset=0` lists blogs with pagination.
- `DELETE /blogs/{slug}` deletes a blog; requires the admin bearer token.

Blog content is stored as Markdown text, so images belong directly in `content` at the position where they should appear. For example:

```markdown
Intro paragraph.

![Challenge diagram](https://cdn.example.com/challenge.webp)

Next paragraph.
```

YouTube videos can be embedded in the same position with `@[youtube](https://youtu.be/VIDEO_ID)`. The composer can insert this marker from a YouTube watch, share, or Shorts URL. The frontend embeds from YouTube's privacy-enhanced domain; video files are not uploaded to R2.

The admin composer can upload an image directly to R2 and inserts the returned URL as `![alt text](public-image-url)` at the cursor. The public UI renders `content` with a Markdown renderer; it does not need to guess placement from `media_urls`. That field is only for a separate media list when needed. Only upload content intended to be public, and sanitize rendered Markdown/HTML in any other frontend.

For deployment, run one Uvicorn worker behind Nginx on the Oracle VM, terminate TLS at Nginx, and restrict access to the admin UI separately. The API token protects write endpoints even if their URLs are known. Back up the SQLite database as part of deployment operations.

## Test

```powershell
py -m pip install -r requirements-dev.txt
py -m pytest
```