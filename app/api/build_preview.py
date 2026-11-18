"""A build's website, previewed.

Served from the office, so it is sandboxed: the page runs in an opaque origin
that can run its own scripts and print, but cannot read the office, call its
API or carry the owner's session. That is also why it is reached by a private
link, not the session cookie: a sandboxed page sends none.
"""

from __future__ import annotations

import hmac
import mimetypes
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy.orm import Session, sessionmaker

from app import builds
from app.config import Settings
from app.db.models import Run
from app.live_build import _events, clone_of, this_attempt


def build_preview_router(session_factory: sessionmaker[Session], settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/previews/{run_id}/{token}/v/{commit}/{path:path}")
    def preview_version(run_id: UUID, token: str, commit: str, path: str) -> Response:
        """One version of the website, as it was when that version was made."""
        key = _allowed(run_id, token)
        folder = builds.folder_for(settings, key)
        try:
            version = builds.version_of(folder, commit)
            data = builds.read_at(folder, version["commit"], path or "index.html")
        except builds.BuildError:
            raise HTTPException(status_code=404, detail="Not found") from None
        return _served(data, path or "index.html")

    @router.get("/previews/{run_id}/{token}/live/{path:path}")
    def preview_live(run_id: UUID, token: str, path: str) -> Response:
        """The website while it is being written, from the attempt clone.

        Read only, sandboxed like every preview. Its pages get a small script
        that lets the owner point at something to comment on; the page can only
        post what was clicked to the office's window, which treats it as text.
        """
        _allowed(run_id, token)

        with session_factory() as session:
            clone = clone_of(this_attempt(_events(session, run_id)), settings.engineering_root)
        if clone is None:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            data, target = builds.read_file(clone, path or "index.html")
        except builds.BuildError:
            raise HTTPException(status_code=404, detail="Not found") from None
        if target.suffix.lower() in (".html", ".htm"):
            data = _with_pointer(data)
        return _served(data, target.name)

    def _allowed(run_id: UUID, token: str) -> str:
        secret = settings.owner_api_token.get_secret_value() if settings.owner_api_token else ""
        expected = builds.preview_token(secret, run_id) if secret else ""
        if not expected or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=404, detail="Not found")
        with session_factory() as session:
            run = session.get(Run, run_id)
            key = (run.state_json or {}).get("build_folder") if run is not None else None
        if not key:
            raise HTTPException(status_code=404, detail="Not found")
        return str(key)

    def _served(data: bytes, name: str) -> Response:
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return Response(
            content=data,
            media_type=kind,
            headers={
                "Content-Security-Policy": builds.PREVIEW_POLICY,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    @router.get("/previews/{run_id}/{token}/{path:path}")
    def preview(run_id: UUID, token: str, path: str) -> Response:
        secret = settings.owner_api_token.get_secret_value() if settings.owner_api_token else ""
        expected = builds.preview_token(secret, run_id) if secret else ""
        if not expected or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=404, detail="Not found")
        with session_factory() as session:
            run = session.get(Run, run_id)
            key = (run.state_json or {}).get("build_folder") if run is not None else None
        if not key:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            data, target = builds.read_file(
                builds.folder_for(settings, str(key)), path or "index.html"
            )
        except builds.BuildError:
            raise HTTPException(status_code=404, detail="Not found") from None
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return Response(
            content=data,
            media_type=kind,
            headers={
                "Content-Security-Policy": builds.PREVIEW_POLICY,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    return router


# Lets the owner point at something on the live page. It runs inside the
# sandboxed page and can do one thing: tell the office's window what was clicked.
POINTER = b"""<script>(() => {
  let on = false, last = null;
  const mark = (n) => {
    if (last) last.style.outline = last.__o || "";
    last = n;
    if (n) { n.__o = n.style.outline; n.style.outline = "2px solid #f97316"; }
  };
  addEventListener("message", (e) => {
    if (e.source !== parent || !e.data || e.data.officePick === undefined) return;
    on = !!e.data.officePick;
    document.documentElement.style.cursor = on ? "crosshair" : "";
    if (!on) mark(null);
  });
  addEventListener("mouseover", (e) => { if (on) mark(e.target); }, true);
  addEventListener("click", (e) => {
    if (!on) return;
    e.preventDefault(); e.stopPropagation();
    const t = e.target, path = [];
    for (let n = t; n && n.nodeType === 1 && path.length < 5; n = n.parentElement) {
      let s = n.tagName.toLowerCase();
      if (n.id) s += "#" + n.id;
      else if (n.classList && n.classList.length) s += "." + [...n.classList].slice(0, 2).join(".");
      path.unshift(s);
    }
    const text = (t.innerText || t.alt || t.title || "").trim().slice(0, 160);
    const picked = { tag: t.tagName.toLowerCase(), text, selector: path.join(" > ") };
    parent.postMessage({ officePicked: picked }, "*");
    on = false; document.documentElement.style.cursor = ""; mark(null);
  }, true);
})();</script>"""


def _with_pointer(data: bytes) -> bytes:
    """The page with the pointer script before its closing body tag (or at its end)."""
    at = data.lower().rfind(b"</body>")
    return data + POINTER if at < 0 else data[:at] + POINTER + data[at:]
