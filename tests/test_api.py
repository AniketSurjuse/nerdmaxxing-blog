from fastapi.testclient import TestClient

from app.main import create_app


def test_homepage_serves_blog_interface(tmp_path):
    app = create_app(database_path=tmp_path / "blogs.db", admin_token="test-admin-token")

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Challenge Library" in response.text


def test_blog_read_and_admin_workflow(tmp_path):
    app = create_app(database_path=tmp_path / "blogs.db", admin_token="test-admin-token")

    with TestClient(app) as client:
        payload = {
            "title": "Getting Started",
            "summary": "A short guide",
            "content": "Intro\n\n@[youtube](https://youtu.be/dQw4w9WgXcQ)\n\n![Diagram](https://cdn.example.com/diagram.webp)\n\nNext step",
            "topic": "Python",
            "media_urls": ["https://cdn.example.com/intro.mp4"],
        }

        assert client.post("/blogs", json=payload).status_code == 401

        headers = {"Authorization": "Bearer test-admin-token"}
        created = client.post("/blogs", json=payload, headers=headers)
        assert created.status_code == 201
        assert created.json()["slug"] == "getting-started"
        assert created.json()["media_urls"] == ["https://cdn.example.com/intro.mp4"]

        assert client.get("/blogs/getting-started").json()["content"] == payload["content"]
        search = client.get("/blogs/search", params={"topic": "python"})
        assert search.json()["total"] == 1
        assert search.json()["items"][0]["slug"] == "getting-started"
        assert client.get("/blogs").json()["total"] == 1

        assert client.delete("/blogs/getting-started").status_code == 401
        assert client.delete("/blogs/getting-started", headers=headers).status_code == 204
        assert client.get("/blogs/getting-started").status_code == 404


def test_admin_mutations_are_disabled_without_token(tmp_path):
    app = create_app(database_path=tmp_path / "blogs.db", admin_token="")

    with TestClient(app) as client:
        response = client.post(
            "/blogs",
            json={"title": "A blog", "content": "Text", "topic": "Challenges"},
        )

    assert response.status_code == 503


def test_image_upload_requires_admin_and_stores_to_r2(tmp_path, monkeypatch):
    app = create_app(
        database_path=tmp_path / "blogs.db",
        admin_token="test-admin-token",
        r2_config={
            "R2_ACCOUNT_ID": "test-account",
            "R2_ACCESS_KEY_ID": "test-access-key",
            "R2_SECRET_ACCESS_KEY": "test-secret-key",
            "R2_BUCKET_NAME": "test-bucket",
            "R2_PUBLIC_BASE_URL": "https://images.example.com",
        },
    )
    uploads = []

    class FakeR2Client:
        def put_object(self, **kwargs):
            uploads.append({**kwargs, "Body": kwargs["Body"].read()})

    monkeypatch.setattr("app.main.create_r2_client", lambda _: FakeR2Client())
    image = b"\x89PNG\r\n\x1a\n" + b"test-image-data"

    with TestClient(app) as client:
        unauthorized = client.put("/media", content=image, headers={"Content-Type": "image/png"})
        invalid_image = client.put(
            "/media",
            content=b"not an image",
            headers={
                "Authorization": "Bearer test-admin-token",
                "Content-Type": "image/png",
            },
        )
        uploaded = client.put(
            "/media",
            content=image,
            headers={
                "Authorization": "Bearer test-admin-token",
                "Content-Type": "image/png",
            },
        )

    assert unauthorized.status_code == 401
    assert invalid_image.status_code == 415
    assert uploaded.status_code == 201
    assert uploaded.json()["url"].startswith("https://images.example.com/images/")
    assert uploads[0]["Bucket"] == "test-bucket"
    assert uploads[0]["ContentType"] == "image/png"
    assert uploads[0]["Body"] == image


def test_text_search_ranks_title_matches_before_content_matches(tmp_path):
    app = create_app(database_path=tmp_path / "blogs.db", admin_token="test-admin-token")
    headers = {"Authorization": "Bearer test-admin-token"}

    with TestClient(app) as client:
        content_match = client.post(
            "/blogs",
            headers=headers,
            json={
                "title": "Making Progress",
                "content": "A deep dive into lichens.",
                "topic": "Outdoors",
            },
        )
        title_match = client.post(
            "/blogs",
            headers=headers,
            json={
                "title": "Lichen Field Guide",
                "content": "A short field guide.",
                "topic": "Nature",
            },
        )

        assert content_match.status_code == 201
        assert title_match.status_code == 201

        response = client.get("/blogs/search", params={"q": "LiChEn"})
        assert response.status_code == 200
        assert response.json()["total"] == 2
        assert [blog["slug"] for blog in response.json()["items"]] == [
            "lichen-field-guide",
            "making-progress",
        ]

        topic_response = client.get("/blogs/search", params={"topic": "nature"})
        assert [blog["slug"] for blog in topic_response.json()["items"]] == ["lichen-field-guide"]