"""Confined local-document extraction, run in the reader service."""

import io
import zipfile
from pathlib import Path

import pytest

from app.sources.reader import IndexLimits, index_root


def docx(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body></w:document>",
        )
    return buffer.getvalue()


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    (root / "briefs").mkdir(parents=True)
    (root / "briefs" / "q3.md").write_text("# Q3\nRevenue grew 12%.")
    (root / "notes.txt").write_text("Customer interviews: pricing is confusing.")
    (root / "plan.docx").write_bytes(docx("Launch in Europe first"))
    (root / "scan.pdf").write_bytes(b"%PDF-1.4 binary")
    (root / "deck.pptx").write_bytes(b"slides")
    (root / ".env").write_text("SECRET=1")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "x.md").write_text("hidden")
    (root / "id_rsa").write_text("private key")
    (root / "server.pem").write_text("cert")
    (tmp_path / "outside.md").write_text("outside the root")
    (root / "escape.md").symlink_to(tmp_path / "outside.md")
    return root


def by_path(result: dict) -> dict:  # type: ignore[type-arg]
    return {document["path"]: document for document in result["documents"]}


def test_supported_documents_are_extracted_with_provenance(library: Path) -> None:
    result = index_root(library.parent, "library")
    documents = by_path(result)
    assert set(documents) == {"briefs/q3.md", "notes.txt", "plan.docx"}
    assert "Revenue grew 12%" in documents["briefs/q3.md"]["text"]
    assert "Launch in Europe first\nSecond paragraph" in documents["plan.docx"]["text"]
    assert len(documents["notes.txt"]["sha256"]) == 64
    assert documents["notes.txt"]["size_bytes"] > 0


def test_unsupported_and_refused_files_are_reported_not_read(library: Path) -> None:
    skipped = {
        item["path"]: item["reason"] for item in index_root(library.parent, "library")["skipped"]
    }
    assert skipped["scan.pdf"] == "unreadable"  # a corrupt PDF, not an unsupported one
    assert skipped["deck.pptx"] == "unsupported_format"
    assert skipped["id_rsa"] == "sensitive_file"
    assert skipped["server.pem"] == "sensitive_file"
    assert skipped["escape.md"] == "outside_root"
    assert ".env" not in skipped and ".hidden/x.md" not in skipped  # hidden: never listed
    assert "outside the root" not in str(index_root(library.parent, "library"))


@pytest.mark.parametrize("root", ["../", "/etc", "library/../../", "", "."])
def test_a_root_outside_the_mount_is_refused(library: Path, root: str) -> None:
    result = index_root(library.parent, root)
    assert result["error"] == "root_outside_mount"
    assert result["documents"] == []


def test_a_missing_root_is_reported(library: Path) -> None:
    assert index_root(library.parent, "absent")["error"] == "root_not_found"


def test_limits_bound_what_is_read(library: Path) -> None:
    (library / "big.txt").write_text("x" * 5000)
    result = index_root(library.parent, "library", IndexLimits(max_file_bytes=1000, max_files=2))
    skipped = {item["path"]: item["reason"] for item in result["skipped"]}
    assert skipped["big.txt"] == "too_large"
    assert len(result["documents"]) == 2
    assert result["truncated"] is True


def test_a_root_can_be_a_subfolder(library: Path) -> None:
    result = index_root(library.parent, "library/briefs")
    assert [d["path"] for d in result["documents"]] == ["q3.md"]


def test_a_malformed_docx_is_skipped_not_fatal(library: Path) -> None:
    (library / "broken.docx").write_bytes(b"not a zip")
    skipped = {
        item["path"]: item["reason"] for item in index_root(library.parent, "library")["skipped"]
    }
    assert skipped["broken.docx"] == "unreadable"


def pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one text line per page (empty string: no text layer)."""
    objects: list[bytes] = []
    kids = []
    font_id = 3
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")  # pages placeholder, filled below
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for text in pages:
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        content_id = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font_id, content_id)
        )
        kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids),
        len(kids),
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_pdf_text_is_extracted_page_by_page(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "report.pdf").write_bytes(pdf(["Revenue grew 12 percent", "Churn fell to 2 percent"]))
    documents = by_path(index_root(tmp_path, "docs"))
    text = documents["report.pdf"]["text"]
    assert "Revenue grew 12 percent" in text
    assert "Churn fell to 2 percent" in text
    assert text.index("Revenue") < text.index("Churn")


def test_blank_pages_are_reported_when_ocr_finds_nothing(tmp_path: Path) -> None:
    needs_tesseract()
    root = tmp_path / "docs"
    root.mkdir()
    (root / "blank.pdf").write_bytes(pdf(["", ""]))
    result = index_root(tmp_path, "docs")
    skipped = {item["path"]: item["reason"] for item in result["skipped"]}
    assert skipped["blank.pdf"] == "ocr_found_no_text"


def test_a_corrupt_pdf_is_skipped_not_fatal(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
    (root / "fine.md").write_text("still indexed")
    result = index_root(tmp_path, "docs")
    skipped = {item["path"]: item["reason"] for item in result["skipped"]}
    assert skipped["broken.pdf"] == "unreadable"
    assert "fine.md" in by_path(result)


def test_pdf_pages_beyond_the_limit_are_not_read(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "long.pdf").write_bytes(pdf([f"Page {n}" for n in range(1, 6)]))
    result = index_root(tmp_path, "docs", IndexLimits(max_pdf_pages=2))
    text = by_path(result)["long.pdf"]["text"]
    assert "Page 2" in text and "Page 3" not in text
    assert by_path(result)["long.pdf"]["pages_truncated"] is True


def test_a_pdf_that_takes_too_long_is_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.sources.reader as reader

    root = tmp_path / "docs"
    root.mkdir()
    (root / "slow.pdf").write_bytes(pdf(["x"]))
    monkeypatch.setattr(reader, "_pdf_worker", reader._sleeping_pdf_worker_for_tests)
    result = index_root(tmp_path, "docs", IndexLimits(pdf_timeout_seconds=0.5))
    skipped = {item["path"]: item["reason"] for item in result["skipped"]}
    assert skipped["slow.pdf"] == "pdf_timeout"


def test_subfolders_of_the_mount_are_listed_for_choosing(tmp_path: Path) -> None:
    from app.sources.reader import list_folders

    (tmp_path / "research" / "2026").mkdir(parents=True)
    (tmp_path / "finance").mkdir()
    (tmp_path / ".private").mkdir()
    (tmp_path / "loose.txt").write_text("x")
    assert list_folders(tmp_path) == ["finance", "research", "research/2026"]


def scanned_pdf(text: str) -> bytes:
    """An image-only PDF: a rendered text page embedded as a picture, no text layer."""
    import io

    import pypdfium2 as pdfium

    source = pdfium.PdfDocument(io.BytesIO(pdf([text])))
    bitmap = source[0].render(scale=3, grayscale=True)
    scanned = pdfium.PdfDocument.new()
    page = scanned.new_page(612, 792)
    image = pdfium.PdfImage.new(scanned)
    image.set_bitmap(bitmap)
    image.set_matrix(pdfium.PdfMatrix().scale(612, 792))
    page.insert_obj(image)
    page.gen_content()
    buffer = io.BytesIO()
    scanned.save(buffer)
    return buffer.getvalue()


def needs_tesseract() -> None:
    import shutil

    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed; this suite runs in the reader image")


def test_a_scanned_pdf_is_read_with_ocr(tmp_path: Path) -> None:
    needs_tesseract()
    root = tmp_path / "docs"
    root.mkdir()
    (root / "contract.pdf").write_bytes(scanned_pdf("Renewal fee is 4200 euros per year"))
    documents = by_path(index_root(tmp_path, "docs"))
    text = documents["contract.pdf"]["text"]
    assert "Renewal fee" in text and "4200" in text
    assert documents["contract.pdf"]["ocr"] is True
    assert text.startswith("[Text recognised by OCR")


def test_a_text_pdf_is_not_sent_to_ocr(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "report.pdf").write_bytes(pdf(["Revenue grew 12 percent"]))
    assert by_path(index_root(tmp_path, "docs"))["report.pdf"]["ocr"] is False


def test_ocr_reads_at_most_the_page_limit(tmp_path: Path) -> None:
    needs_tesseract()
    import io

    import pypdfium2 as pdfium

    root = tmp_path / "docs"
    root.mkdir()
    merged = pdfium.PdfDocument.new()
    for n in range(3):
        merged.import_pages(pdfium.PdfDocument(io.BytesIO(scanned_pdf(f"Page number {n + 1}"))))
    buffer = io.BytesIO()
    merged.save(buffer)
    (root / "scan.pdf").write_bytes(buffer.getvalue())
    document = by_path(index_root(tmp_path, "docs", IndexLimits(max_ocr_pages=2)))["scan.pdf"]
    assert "number 2" in document["text"] and "number 3" not in document["text"]
    assert document["pages_truncated"] is True


def test_without_an_ocr_engine_a_scan_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.sources.reader as reader

    monkeypatch.setattr(reader, "TESSERACT", "/nonexistent/tesseract")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "scan.pdf").write_bytes(pdf(["", ""]))
    skipped = {item["path"]: item["reason"] for item in index_root(tmp_path, "docs")["skipped"]}
    assert skipped["scan.pdf"] == "no_text_layer_ocr_needed"
