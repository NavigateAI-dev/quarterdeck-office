"""Putting a website online, against fake Netlify and GitHub."""

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import hosting


def site(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<h1>Colour</h1>")
    (tmp_path / "check.sh").write_text("true")
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "cat.svg").write_text("<svg/>")
    return tmp_path


def test_the_website_is_its_files_without_the_office_ones(tmp_path: Path) -> None:
    files = dict(hosting.site_files(hosting.site_root(site(tmp_path))))
    assert sorted(files) == ["images/cat.svg", "index.html"]


def test_a_built_folder_is_the_website_when_there_is_one(tmp_path: Path) -> None:
    site(tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>Built</h1>")
    assert hosting.site_root(tmp_path) == tmp_path / "dist"


def test_no_index_is_no_website(tmp_path: Path) -> None:
    with pytest.raises(hosting.HostingError):
        hosting.site_root(tmp_path)


def client(answer: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


async def test_netlify_makes_a_site_and_uploads_the_zip(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer nf"
        if request.url.path == "/api/v1/sites" and request.method == "POST":
            return httpx.Response(201, json={"id": "s1"})
        if request.url.path.endswith("/deploys"):
            assert request.headers["content-type"] == "application/zip"
            return httpx.Response(200, json={"id": "d1"})
        return httpx.Response(200, json={"ssl_url": "https://brave-cat.netlify.app"})

    files = hosting.site_files(site(tmp_path))
    done = await hosting.to_netlify("nf", files, None, client(answer))
    assert done == {"target": "netlify", "site_id": "s1", "url": "https://brave-cat.netlify.app"}
    again = await hosting.to_netlify("nf", files, "s1", client(answer))
    assert again["site_id"] == "s1"
    assert seen.count(("POST", "/api/v1/sites")) == 1, "publishing again updates the same site"


async def test_github_makes_a_repository_uploads_and_turns_pages_on(tmp_path: Path) -> None:
    uploaded: dict[str, bytes] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user":
            return httpx.Response(200, json={"login": "Alex"})
        if path == "/user/repos":
            assert json.loads(request.content)["name"] == "colouring-pages"
            return httpx.Response(201, json={})
        if "/contents/" in path and request.method == "GET":
            return httpx.Response(404)
        if "/contents/" in path and request.method == "PUT":
            body = json.loads(request.content)
            uploaded[path.split("/contents/")[1]] = base64.b64decode(body["content"])
            return httpx.Response(201, json={})
        if path.endswith("/pages"):
            return httpx.Response(201, json={})
        return httpx.Response(404)

    files = hosting.site_files(site(tmp_path))
    done = await hosting.to_github("gh", "Colouring Pages!", files, None, client(answer))
    assert done["url"] == "https://alex.github.io/colouring-pages/"
    assert done["repository"] == "Alex/colouring-pages"
    assert uploaded["index.html"] == b"<h1>Colour</h1>"


async def test_a_repository_name_already_used_is_never_written(tmp_path: Path) -> None:
    writes: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "alex"})
        if request.url.path == "/user/repos":
            return httpx.Response(422, json={"message": "name already exists"})
        writes.append(request.url.path)
        return httpx.Response(201, json={})

    with pytest.raises(hosting.HostingError) as taken:
        await hosting.to_github(
            "gh", "my-site", hosting.site_files(site(tmp_path)), None, client(answer)
        )
    assert taken.value.code == "repository_name_taken" and writes == []


async def test_a_refused_token_says_so(tmp_path: Path) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(hosting.HostingError) as refused:
        await hosting.to_netlify("bad", hosting.site_files(site(tmp_path)), None, client(answer))
    assert refused.value.code == "token_refused"
