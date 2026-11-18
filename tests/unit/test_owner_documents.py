"""A long chat reply, opened in the editor as a document of its own."""

from app.documents import Bullets, Paragraph
from app.owner_documents import from_text

REPLY = """Using general knowledge, here is a short customizable template:

Privacy Policy

Effective date: [Date]

[Website/Company Name] respects your privacy. We may collect information
you provide directly.

What we collect
- your name
- your email address

1. Ask us
2. We answer within 30 days"""


def test_a_reply_becomes_headed_sections_paragraphs_and_lists() -> None:
    document = from_text(REPLY, "Privacy terms")
    assert document.title == "Privacy terms"
    headings = [section.heading for section in document.sections]
    assert headings == ["", "Privacy Policy", "What we collect"]
    opening = document.sections[0].blocks
    assert opening == [Paragraph("Using general knowledge, here is a short customizable template:")]
    policy = document.sections[1].blocks
    assert policy[0] == Paragraph("Effective date: [Date]"), "a line with a colon is not a heading"
    assert policy[1].text.startswith("[Website/Company Name] respects your privacy. We may")
    collected = document.sections[2].blocks
    assert collected[0] == Bullets(items=["your name", "your email address"])
    assert collected[1] == Bullets(items=["Ask us", "We answer within 30 days"], ordered=True)


def test_markdown_headings_are_headings() -> None:
    document = from_text("## Scope\nWe cover the website.", "t")
    assert [s.heading for s in document.sections] == ["Scope"]


def test_an_empty_reply_is_still_a_document() -> None:
    document = from_text("   ", "t")
    assert document.sections and document.sections[0].blocks == [Paragraph("")]
