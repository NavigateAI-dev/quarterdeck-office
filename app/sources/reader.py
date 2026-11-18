"""Read selected local folders inside the `sources` service (delivery slice 2).

The owner's folder is mounted read-only into a service with no network and no
secrets; only this module parses those files. The API asks for an index of a
selected root through an exchange volume and never mounts the folder itself.

Confinement: a root must be a subfolder of the mount; every file is resolved
and must stay inside that root (symlinks included); hidden entries are never
listed; key, certificate and environment files are refused by name; sizes and
counts are bounded. Unsupported formats are reported with a reason, never
silently dropped.
"""

import asyncio
import hashlib
import io
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree  # noqa: S405 - parses only a docx's own XML part

from app.archive.adapters import parse_export

TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".csv", ".json", ".tsv"})
DOCX_SUFFIX = ".docx"
PDF_SUFFIX = ".pdf"
FOLDER_DEPTH = 3
MAX_FOLDERS = 500
SENSITIVE_NAMES = re.compile(
    r"^(id_rsa|id_ed25519|id_ecdsa|id_dsa)(\.pub)?$|\.(pem|key|p12|pfx|keystore|kdbx)$",
    re.IGNORECASE,
)
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
EXCHANGE_JOBS = "jobs"
# Where the API stages files the owner uploaded from their computer.
STAGING = "staged"
# Where AI tools' exports wait to be parsed, and where the plain lines go.
ARCHIVE = "archive"
HEARTBEAT = "reader.heartbeat"
PDF_REASONS = frozenset(
    {"pdf_timeout", "unreadable", "no_text_layer_ocr_needed", "ocr_found_no_text"}
)
TESSERACT = "tesseract"
OCR_DPI = 200
OCR_PAGE_SECONDS = 60
# Marks recognised text so readers and agents know it is not the document's own layer.
OCR_NOTICE = "[Text recognised by OCR from scanned pages; may contain recognition errors.]"
# Pictures staged with a document by the API, under IMAGES.
IMAGES = "images"
OCR_LANGUAGES = "chi_sim+chi_tra+eng"
OCR_IMAGE_SECONDS = 60
MAX_IMAGES = 9
IMAGE_NAME = re.compile(r"^[0-9]{1,2}\.img$")
# Tesseract spaces out Chinese characters; a space between two of them is noise.
CJK_GAP = re.compile(r"(?<=[\u3000-\u9fff\uff00-\uffef]) +(?=[\u3000-\u9fff\uff00-\uffef])")


@dataclass(frozen=True)
class IndexLimits:
    max_files: int = 500
    max_file_bytes: int = 5 * 1024 * 1024
    max_text_chars: int = 200_000
    max_pdf_pages: int = 300
    # OCR is slow: fewer pages, more time, and only when there is no text layer.
    max_ocr_pages: int = 20
    pdf_timeout_seconds: float = 120.0


def _resolve_root(mount: Path, root: str) -> Path | None:
    relative = PurePosixPath(root)
    if not root or relative.is_absolute() or ".." in relative.parts or root in (".", "./"):
        return None
    base = mount.resolve()
    candidate = (mount / relative).resolve()
    if candidate == base or base not in candidate.parents:
        return None
    return candidate


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml = archive.read("word/document.xml")
    tree = ElementTree.fromstring(xml)  # noqa: S314 - bounded, from a size-limited file
    paragraphs = []
    for paragraph in tree.iter(f"{WORD_NS}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{WORD_NS}t"))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _ocr_page(bitmap: Any, tesseract: str) -> str:
    """Recognise one rendered page with Tesseract, fed as PGM on stdin."""
    width, height, stride = bitmap.width, bitmap.height, bitmap.stride
    raw = bytes(bitmap.buffer)
    rows = b"".join(raw[row * stride : row * stride + width] for row in range(height))
    image = f"P5 {width} {height} 255\n".encode() + rows
    result = subprocess.run(  # noqa: S603 - fixed binary, argument vector, no shell
        [tesseract, "stdin", "stdout", "-l", "eng", "--psm", "3"],
        input=image,
        capture_output=True,
        timeout=OCR_PAGE_SECONDS,
        check=False,
    )
    return result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else ""


def _picture_text(raw: str) -> str:
    lines = [CJK_GAP.sub("", " ".join(line.split())) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def ocr_images(folder: Path, tesseract: str = TESSERACT) -> dict[str, Any]:
    """The text in each staged picture, in name order ("0.img", "1.img", ...).

    Tesseract reads each file in its own process with a time limit, so a
    hostile picture can only fail itself. An unreadable picture gives "".
    """
    if shutil.which(tesseract) is None:
        return {"error": "ocr_unavailable"}
    names = sorted(
        (path for path in folder.iterdir() if IMAGE_NAME.match(path.name)),
        key=lambda path: int(path.name.split(".")[0]),
    )[:MAX_IMAGES]
    texts: list[str] = []
    for path in names:
        try:
            result = subprocess.run(  # noqa: S603 - fixed binary, argument vector, no shell
                [tesseract, str(path), "stdout", "-l", OCR_LANGUAGES, "--psm", "3"],
                capture_output=True,
                timeout=OCR_IMAGE_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            texts.append("")
            continue
        raw = result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else ""
        texts.append(_picture_text(raw))
    return {"texts": texts}


def _ocr(data: bytes, max_pages: int, tesseract: str) -> tuple[list[str], bool]:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    document = pdfium.PdfDocument(data)
    count = len(document)
    texts = [
        _ocr_page(document[i].render(scale=OCR_DPI / 72, grayscale=True), tesseract)
        for i in range(min(count, max_pages))
    ]
    return texts, count > max_pages


def _pdf_worker(
    data: bytes, max_pages: int, connection: Any, max_ocr_pages: int, tesseract: str
) -> None:
    """Runs in a child process: a hostile PDF can hang or crash only this."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages
        texts = [(pages[i].extract_text() or "") for i in range(min(len(pages), max_pages))]
        if any(text.strip() for text in texts):
            connection.send(("ok", texts, len(pages) > max_pages, False))
        elif shutil.which(tesseract) is None:
            connection.send(("no_text", [], False, False))
        else:
            ocr_texts, truncated = _ocr(data, max_ocr_pages, tesseract)
            connection.send(("ok", ocr_texts, truncated, True))
    except Exception:  # noqa: BLE001 - any parser failure means "unreadable"
        connection.send(("error", [], False, False))
    finally:
        connection.close()


def _sleeping_pdf_worker_for_tests(
    data: bytes, max_pages: int, connection: Any, max_ocr_pages: int, tesseract: str
) -> None:
    time.sleep(30)


def _pdf_text(data: bytes, limits: IndexLimits) -> tuple[str, bool, bool]:
    """Extract text page by page in a bounded child process.

    Raises ValueError with a reason code when the PDF cannot be used.
    """
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_pdf_worker,
        # The OCR engine is passed in: a spawned child does not share this module's state.
        args=(data, limits.max_pdf_pages, sender, limits.max_ocr_pages, TESSERACT),
        daemon=True,
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(limits.pdf_timeout_seconds):
            raise ValueError("pdf_timeout")
        status, texts, truncated, ocr = receiver.recv()
    except EOFError:
        raise ValueError("unreadable") from None
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=5)
        receiver.close()
    if status == "no_text":
        # Pages exist but carry no text layer, and no OCR engine is available.
        raise ValueError("no_text_layer_ocr_needed")
    if status != "ok":
        raise ValueError("unreadable")
    text = "\n\n".join(page.strip() for page in texts if page.strip())
    if not text:
        raise ValueError("ocr_found_no_text" if ocr else "no_text_layer_ocr_needed")
    if ocr:
        text = f"{OCR_NOTICE}\n\n{text}"
    return text, bool(truncated), bool(ocr)


def list_folders(mount: Path) -> list[str]:
    """Visible subfolders of the mount, a few levels deep, for the owner to choose."""
    base = mount.resolve()
    folders: list[str] = []
    for directory, subdirectories, _ in os.walk(base, followlinks=False):
        depth = len(Path(directory).relative_to(base).parts)
        subdirectories[:] = sorted(d for d in subdirectories if not d.startswith("."))
        if depth >= FOLDER_DEPTH:
            subdirectories[:] = []
        for name in subdirectories:
            folders.append((Path(directory) / name).relative_to(base).as_posix())
            if len(folders) >= MAX_FOLDERS:
                return sorted(folders)
    return sorted(folders)


def _visible_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        subdirectories[:] = sorted(d for d in subdirectories if not d.startswith("."))
        files.extend(Path(directory) / name for name in sorted(names) if not name.startswith("."))
    return files


def index_root(mount: Path, root: str, limits: IndexLimits | None = None) -> dict[str, Any]:
    bounds = limits or IndexLimits()
    result: dict[str, Any] = {"documents": [], "skipped": [], "truncated": False, "error": None}
    resolved = _resolve_root(mount, root)
    if resolved is None:
        result["error"] = "root_outside_mount"
        return result
    if not resolved.is_dir():
        result["error"] = "root_not_found"
        return result
    for path in _visible_files(resolved):
        relative = path.relative_to(resolved).as_posix()

        def skip(reason: str, relative: str = relative) -> None:
            result["skipped"].append({"path": relative, "reason": reason})

        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            skip("unreadable")
            continue
        if resolved not in real.parents:
            skip("outside_root")
            continue
        if SENSITIVE_NAMES.search(path.name):
            skip("sensitive_file")
            continue
        suffix = path.suffix.lower()
        if suffix not in TEXT_SUFFIXES and suffix not in (DOCX_SUFFIX, PDF_SUFFIX):
            skip("unsupported_format")
            continue
        size = real.stat().st_size
        if size > bounds.max_file_bytes:
            skip("too_large")
            continue
        if len(result["documents"]) >= bounds.max_files:
            result["truncated"] = True
            continue
        data = real.read_bytes()
        pages_truncated = False
        ocr = False
        try:
            if suffix == PDF_SUFFIX:
                text, pages_truncated, ocr = _pdf_text(data, bounds)
            elif suffix == DOCX_SUFFIX:
                text = _docx_text(data)
            else:
                text = data.decode("utf-8")
        except ValueError as reason:
            # UnicodeDecodeError is a ValueError too; it carries no reason code.
            code = str(reason) if str(reason) in PDF_REASONS else "unreadable"
            skip(code)
            continue
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
            skip("unreadable")
            continue
        result["documents"].append(
            {
                "pages_truncated": pages_truncated,
                "ocr": ocr,
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": size,
                "modified_at": datetime.fromtimestamp(real.stat().st_mtime, UTC).isoformat(),
                "text": text[: bounds.max_text_chars],
            }
        )
    return result


# ---- A space's folder, read live ----
# The folder as it is now, not a copy: listed, searched and read on request,
# with the same refusals as indexing (inside the folder after symlinks, no
# hidden or sensitive file, known formats, size limits).

LIVE_FILES = 300
SEARCH_HITS = 30
SEARCH_FILES = 2000
SEARCH_LINE_CHARS = 240
READ_CHARS = 12_000


def _live_file(root: Path, path: Path, limits: IndexLimits) -> str | None:
    """Why this file may not be read live, or None when it may."""
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return "unreadable"
    if root not in real.parents:
        return "outside_root"
    if any(part.startswith(".") for part in path.relative_to(root).parts):
        return "hidden_file"
    if SENSITIVE_NAMES.search(path.name):
        return "sensitive_file"
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES and suffix not in (DOCX_SUFFIX, PDF_SUFFIX):
        return "unsupported_format"
    if real.stat().st_size > limits.max_file_bytes:
        return "too_large"
    return None


def _live_text(path: Path, limits: IndexLimits) -> str:
    data = path.resolve().read_bytes()
    suffix = path.suffix.lower()
    if suffix == PDF_SUFFIX:
        return _pdf_text(data, limits)[0]
    if suffix == DOCX_SUFFIX:
        return _docx_text(data)
    return data.decode("utf-8")


def live_files(mount: Path, root: str) -> dict[str, Any]:
    """The readable files of a folder, newest first."""
    resolved = _resolve_root(mount, root)
    if resolved is None or not resolved.is_dir():
        return {"error": "root_not_found", "files": []}
    limits = IndexLimits()
    found: list[dict[str, Any]] = []
    for path in _visible_files(resolved):
        if _live_file(resolved, path, limits) is None:
            stat = path.resolve().stat()
            found.append(
                {
                    "path": path.relative_to(resolved).as_posix(),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                }
            )
    found.sort(key=lambda row: str(row["modified_at"]), reverse=True)
    return {"error": None, "files": found[:LIVE_FILES], "more": max(0, len(found) - LIVE_FILES)}


def live_search(mount: Path, root: str, query: str) -> dict[str, Any]:
    """Lines holding every word of the query, in the folder's text and Word files.

    PDFs are not searched (each is read in its own bounded process); they are
    counted so the caller can read one by name."""
    resolved = _resolve_root(mount, root)
    if resolved is None or not resolved.is_dir():
        return {"error": "root_not_found", "hits": []}
    words = [word.lower() for word in query.split() if word.strip()]
    if not words:
        return {"error": "empty_query", "hits": []}
    limits = IndexLimits()
    hits: list[dict[str, Any]] = []
    pdfs = scanned = 0
    for path in _visible_files(resolved):
        if len(hits) >= SEARCH_HITS or scanned >= SEARCH_FILES:
            break
        if _live_file(resolved, path, limits) is not None:
            continue
        if path.suffix.lower() == PDF_SUFFIX:
            pdfs += 1
            continue
        scanned += 1
        try:
            text = _live_text(path, limits)
        except (ValueError, OSError, zipfile.BadZipFile, KeyError, ElementTree.ParseError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            if all(word in lowered for word in words):
                hits.append(
                    {
                        "path": path.relative_to(resolved).as_posix(),
                        "line": number,
                        "text": line.strip()[:SEARCH_LINE_CHARS],
                    }
                )
                if len(hits) >= SEARCH_HITS:
                    break
    return {"error": None, "hits": hits, "pdfs_not_searched": pdfs}


def live_read(mount: Path, root: str, relative: str, offset: int = 0) -> dict[str, Any]:
    """One file of the folder, from `offset`, at most READ_CHARS characters."""
    resolved = _resolve_root(mount, root)
    if resolved is None or not resolved.is_dir():
        return {"error": "root_not_found"}
    wanted = PurePosixPath(relative)
    if not relative or wanted.is_absolute() or ".." in wanted.parts:
        return {"error": "outside_root"}
    path = resolved / wanted
    if not path.is_file():
        return {"error": "file_not_found"}
    limits = IndexLimits()
    refused = _live_file(resolved, path, limits)
    if refused is not None:
        return {"error": refused}
    try:
        text = _live_text(path, limits)
    except ValueError as reason:
        return {"error": str(reason) if str(reason) in PDF_REASONS else "unreadable"}
    except (OSError, zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        return {"error": "unreadable"}
    start = max(0, int(offset))
    return {
        "error": None,
        "path": wanted.as_posix(),
        "text": text[start : start + READ_CHARS],
        "offset": start,
        "total_chars": len(text),
        "more": start + READ_CHARS < len(text),
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload))
    os.replace(temporary, path)


def serve_once(mount: Path, exchange: Path) -> bool:
    """Handle the oldest pending index job, if any. True if one was handled."""
    jobs = exchange / EXCHANGE_JOBS
    pending = sorted(jobs.glob("*.job.json"), key=lambda path: path.stat().st_mtime)
    if not pending:
        return False
    claimed = pending[0].with_suffix(".running")
    try:
        os.replace(pending[0], claimed)
    except FileNotFoundError:
        return False
    try:
        job = json.loads(claimed.read_text())
        job_id = str(job["id"])
        if not job_id.isalnum():
            raise ValueError("job_id")
        kind = job.get("kind")
        if kind == "list":
            payload: dict[str, Any] = {"folders": list_folders(mount)}
        elif kind == "files":
            payload = live_files(mount, str(job["root"]))
        elif kind == "search":
            payload = live_search(mount, str(job["root"]), str(job["query"]))
        elif kind == "read":
            payload = live_read(
                mount, str(job["root"]), str(job["path"]), int(job.get("offset") or 0)
            )
        elif kind == "archive":
            name = str(job["name"])
            if not name.isalnum():
                raise ValueError("name")
            folder = exchange / ARCHIVE
            payload = parse_export(folder / f"{name}.upload", folder / f"{name}.jsonl")
        elif kind == "ocr_images":
            name = str(job["name"])
            if not name.isalnum():
                raise ValueError("name")
            payload = ocr_images(exchange / IMAGES / name)
        elif kind == "index_staged":
            # Files the owner chose in the browser, staged by the API in the
            # exchange. They are read here, where there is no network and no
            # credential, exactly like files from the mounted folder.
            payload = index_root(exchange / STAGING, str(job["root"]))
        else:
            payload = index_root(mount, str(job["root"]))
    except (OSError, ValueError, KeyError, TypeError):
        claimed.unlink(missing_ok=True)
        return True
    _write_atomic(jobs / f"{job_id}.result.json", payload)
    claimed.unlink(missing_ok=True)
    return True


async def serve_forever(mount: Path, exchange: Path, poll_seconds: float = 0.25) -> None:
    (exchange / EXCHANGE_JOBS).mkdir(parents=True, exist_ok=True)
    while True:
        (exchange / HEARTBEAT).write_text(str(time.time()))
        if not await asyncio.to_thread(serve_once, mount, exchange):
            await asyncio.sleep(poll_seconds)


def main() -> None:
    asyncio.run(
        serve_forever(
            Path(os.environ.get("SOURCES_MOUNT", "/sources")),
            Path(os.environ.get("SOURCES_EXCHANGE", "/exchange")),
        )
    )


if __name__ == "__main__":
    main()
