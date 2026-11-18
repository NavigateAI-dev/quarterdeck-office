"""A finished task must read as a document, not as a JSON dump."""

from app.documents import Bullets, Sources, Table, body_html, document_for, to_html, to_markdown

RESEARCH = {
    "research_question": "Which KYC vendors serve Dutch EMIs?",
    "key_findings": ["eIDAS 2.0 wallets are not yet mandatory", "Three vendors cover NL"],
    "evidence": ["The AFM guidance of 2026 says so"],
    "unknowns": ["Pricing is not published"],
    "confidence": "medium",
    "sources": [
        {"title": "AFM guidance", "url": "https://afm.nl/kyc", "source_type": "primary"},
        {"title": "Vendor page", "url": "https://vendor.example/eidas"},
    ],
    "comparison_table": {
        "columns": ["Vendor", "eIDAS 2.0"],
        "rows": [["Alpha", "yes"], ["Beta", "not documented"]],
        "row_sources": ["https://alpha.example", "https://beta.example"],
    },
}


def test_a_research_report_reads_as_a_document() -> None:
    document = document_for("research_report", RESEARCH)
    headings = [section.heading for section in document.sections]
    assert "Key findings" in headings, headings
    assert "Comparison" in headings and "Sources" in headings

    findings = next(s for s in document.sections if s.heading == "Key findings").blocks[0]
    assert isinstance(findings, Bullets)
    assert findings.items[0].startswith("eIDAS 2.0 wallets")
    # The question belongs in the document, not only in the title.
    asked = next(s for s in document.sections if s.heading == "What was asked")
    assert any("KYC vendors" in getattr(block, "text", "") for block in asked.blocks)


def test_each_comparison_row_keeps_its_own_source() -> None:
    """Pooling sources at the end loses which row each one backs."""
    document = document_for("research_report", RESEARCH)
    table = document.tables()[0]
    assert isinstance(table, Table)
    assert table.columns == ["Vendor", "eIDAS 2.0", "Source"]
    assert table.rows[0] == ["Alpha", "yes", "https://alpha.example"]
    assert table.rows[1][2] == "https://beta.example"


def test_a_source_reads_as_its_title_and_link() -> None:
    document = document_for("research_report", RESEARCH)
    sources = next(s for s in document.sections if s.heading == "Sources").blocks[0]
    assert isinstance(sources, Sources)
    assert sources.items[0] == "AFM guidance — https://afm.nl/kyc"
    assert "{" not in sources.items[0], "a source must not render as a dict"


def test_nothing_in_the_payload_is_silently_dropped() -> None:
    """An artifact from a type this office no longer produces still reads."""
    document = document_for("something_new", {"whatever": "a value", "items": ["a", "b"]})
    rendered = to_markdown(document)
    assert "a value" in rendered
    assert "- a" in rendered and "- b" in rendered


def test_markdown_keeps_the_table_and_escapes_a_pipe() -> None:
    payload = {
        **RESEARCH,
        "comparison_table": {
            "columns": ["Vendor", "Note"],
            "rows": [["Alpha", "a|b"]],
            "row_sources": [],
        },
    }
    rendered = to_markdown(document_for("research_report", payload))
    assert "| Vendor | Note |" in rendered
    assert "a\\|b" in rendered, "an unescaped pipe would end the column early"


def test_html_escapes_every_value() -> None:
    """Payload text is model output and goes onto a page the owner opens."""
    payload = {"question": "<script>alert(1)</script>", "findings": ["<img onerror=x>"]}
    page = to_html(document_for("research_summary", payload))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "<img onerror=x>" not in page
    assert page.startswith("<!doctype html>")


def test_the_body_can_be_shown_inside_the_office() -> None:
    body = body_html(document_for("research_report", RESEARCH))
    assert "<table>" in body and "<h2>" in body
    assert "<!doctype" not in body, "the embedded body carries no page of its own"


def test_a_table_sits_in_a_frame_that_scrolls_sideways() -> None:
    """A wide table scrolls within the reading column instead of spilling past it."""
    body = body_html(document_for("research_report", RESEARCH))
    assert '<div class="table-frame"><table>' in body and "</table></div>" in body


def test_a_word_file_holds_the_headings_and_the_table() -> None:
    """A real .docx, opened again: what the owner sends on must carry the work."""
    import io

    from docx import Document as WordDocument

    from app.documents.office_files import to_docx

    data = to_docx(document_for("research_report", RESEARCH, title="KYC vendors"))
    word = WordDocument(io.BytesIO(data))
    assert word.paragraphs[0].text == "KYC vendors"
    headings = [p.text for p in word.paragraphs if p.style.name.startswith("Heading")]
    assert "Key findings" in headings and "Sources" in headings
    assert len(word.tables) == 1, "the comparison must stay a table"
    header = [cell.text for cell in word.tables[0].rows[0].cells]
    assert header == ["Vendor", "eIDAS 2.0", "Source"]
    assert [cell.text for cell in word.tables[0].rows[1].cells][0] == "Alpha"


def test_a_spreadsheet_puts_the_comparison_on_its_own_sheet() -> None:
    import io

    from openpyxl import load_workbook

    from app.documents.office_files import to_xlsx

    data = to_xlsx(document_for("research_report", RESEARCH, title="KYC vendors"))
    book = load_workbook(io.BytesIO(data))
    assert "Report" in book.sheetnames and "Comparison" in book.sheetnames
    sheet = book["Comparison"]
    assert [cell.value for cell in sheet[1]] == ["Vendor", "eIDAS 2.0", "Source"]
    assert [cell.value for cell in sheet[2]] == ["Alpha", "yes", "https://alpha.example"]
    assert sheet.max_row == 3, "one row per comparison row"


def test_a_document_without_a_table_still_makes_a_workbook() -> None:
    import io

    from openpyxl import load_workbook

    from app.documents.office_files import to_xlsx

    payload = {"question": "What happened?", "findings": ["A thing happened"]}
    book = load_workbook(io.BytesIO(to_xlsx(document_for("research_summary", payload))))
    assert book.sheetnames == ["Report"]
    assert book["Report"]["A1"].value == "What happened?"


ANSWERED = {
    "research_question": "Which KYC vendors serve Dutch EMIs?",
    "answers": [
        {
            "question": "What does the Wwft require for customer due diligence?",
            "status": "answered",
            "answer": "Article 3 requires an institution to perform cliëntenonderzoek.",
            "evidence": ["https://wetten.overheid.nl/BWBR0024282/"],
            "missing": [],
        },
        {
            "question": "Which vendors support the Dutch eID schemes?",
            "status": "unanswered",
            "answer": "The retrieved evidence was vendor marketing, which is not evidence.",
            "evidence": [],
            "missing": ["Vendor documentation naming the supported schemes"],
        },
    ],
    "blocked_sources": [{"url": "https://www.dnb.nl/en/", "status": "http_403"}],
    "key_findings": ["Article 3 states the obligation"],
    "evidence": ["Wwft Article 3"],
    "sources": [{"title": "Wwft", "url": "https://wetten.overheid.nl/BWBR0024282/"}],
    "confidence": "medium",
}


def test_a_report_says_how_much_it_answered_before_what_it_found() -> None:
    """The KYC report listed its own questions as unknowns and read as complete."""
    document = document_for("research_report", ANSWERED)
    assert document.sections[0].heading == "Coverage"
    text = to_markdown(document)
    assert "Answered 1 of 2 questions asked" in text
    assert "1 unanswered" in text


def test_each_question_gets_its_own_section_with_its_evidence() -> None:
    document = document_for("research_report", ANSWERED)
    headings = [section.heading for section in document.sections]
    assert "What does the Wwft require for customer due diligence?" in headings
    assert "Which vendors support the Dutch eID schemes?" in headings
    text = to_markdown(document)
    assert "Status: unanswered" in text
    assert "Still needed:" in text
    # The question is a heading, not a prefix repeated on every finding.
    assert "[What does the Wwft" not in text


def test_a_source_that_refused_us_is_named_as_refused() -> None:
    document = document_for("research_report", ANSWERED)
    text = to_markdown(document)
    assert "Sources that refused us" in text
    assert "https://www.dnb.nl/en/ — http_403" in text


INCOMPLETE = {
    "objective": "Which KYC vendors serve Dutch EMIs?",
    "recommendation": "是。确认启动该项目的调研与方案梳理。",
    "outcome": "decision_recorded",
    "coverage": {
        "acceptance_criteria": ["A comparison of at least 5 vendors", "Three public cases"],
        "questions_asked": 4,
        "questions_answered": 1,
        "unanswered_questions": ["Which vendors support the Dutch eID schemes?"],
        "last_review_verdict": "REVISE",
        "unmet_by_review": ["Vendor comparison lists 2 of the 5 required"],
        "blocked_sources": ["https://www.dnb.nl/en/: http_403"],
        "stopped_reason": "revision_not_converging",
    },
}


def test_an_unfinished_task_says_so_in_its_first_line() -> None:
    """The complaint: a report that reads 'task done, user accept'."""
    document = document_for("final_report", INCOMPLETE)
    assert document.sections[0].heading == "Did this answer the ask?"
    opening = document.sections[0].blocks[0]
    assert "did not fully answer" in getattr(opening, "text", "")
    text = to_markdown(document)
    assert "Questions answered: 1 of 4." in text
    assert "Last review verdict: REVISE." in text
    assert "Vendor comparison lists 2 of the 5 required" in text
    assert "Stopped because: revision_not_converging." in text
    # The recommendation is still there, but it is no longer the first thing.
    assert text.index("Did this answer") < text.index("确认启动")


def test_a_finished_task_is_not_made_to_look_unfinished() -> None:
    complete = {
        **INCOMPLETE,
        "coverage": {
            "acceptance_criteria": ["A comparison of at least 5 vendors"],
            "questions_asked": 2,
            "questions_answered": 2,
            "unanswered_questions": [],
            "last_review_verdict": "PASS",
            "unmet_by_review": [],
            "blocked_sources": [],
            "stopped_reason": "",
        },
    }
    text = to_markdown(document_for("final_report", complete))
    assert "This met what was asked." in text
    assert "did not fully answer" not in text


def test_an_unanswered_part_is_retried_with_what_it_said_was_missing() -> None:
    """A gap is a query, not a conclusion."""
    from app.orchestrator.graph import retry_focus, unanswered

    answered = {"answers": [{"question": "q", "status": "answered", "answer": "a"}]}
    assert unanswered(answered) is False

    gap = {
        "answers": [
            {
                "question": "Which vendors support Dutch eID?",
                "status": "unanswered",
                "answer": "Only marketing pages were retrieved.",
                "missing": ["Vendor documentation naming the schemes"],
            }
        ],
        "blocked_sources": [{"url": "https://www.dnb.nl/en/", "status": "http_403"}],
    }
    assert unanswered(gap) is True
    focus = retry_focus(gap)
    assert "Vendor documentation naming the schemes" in focus
    assert "refused us" in focus and "dnb.nl" in focus


def test_a_part_that_found_nothing_at_all_counts_as_unanswered() -> None:
    from app.orchestrator.graph import unanswered

    assert unanswered({"key_findings": []}) is True
    assert unanswered({"key_findings": ["something"]}) is False


SYNTHESIS = {
    "recommendation": "Prioritise a platform-agnostic readiness offer, then validate it.",
    "inferences": ["Discovery may shift towards assistants", "Brands will need structured data"],
    "facts": ["The objective is to identify opportunities"],
    "assumptions": ["Muse keeps its current trajectory"],
    "unknowns": ["Meta has published no partner programme"],
    "proposed_engineering_scope": "Define a replaceable identity channel interface…",
    "engineering_acceptance_criteria": ["A glossary", "A mapping", "A test plan"],
}


def test_the_answer_comes_first_and_alone() -> None:
    """The answer used to sit in the middle of an engineering log."""
    document = document_for("synthesis", SYNTHESIS, title="Agentic marketing")
    assert [section.heading for section in document.answer()] == ["The answer"]
    answer = document.answer()[0]
    text = " ".join(
        getattr(block, "text", "") + " ".join(getattr(block, "items", []))
        for block in answer.blocks
    )
    assert "platform-agnostic readiness offer" in text
    assert "Discovery may shift towards assistants" in text, "the reasoning carries the answer"


def test_the_working_is_kept_but_out_of_the_way() -> None:
    document = document_for("synthesis", SYNTHESIS, title="Agentic marketing")
    working = [section.heading for section in document.working()]
    assert "Facts" in working and "Unknowns" in working and "Assumptions" in working
    rendered = to_markdown(document)
    # Everything is still there, after a divider that says what follows.
    assert "How it got there" in rendered
    assert rendered.index("The answer") < rendered.index("How it got there")
    assert rendered.index("How it got there") < rendered.index("Unknowns")


def test_engineering_scaffolding_is_not_shown_when_nothing_was_built() -> None:
    """A marketing question carried an engineering scope because V0 always did."""
    rendered = to_markdown(document_for("synthesis", SYNTHESIS, title="Agentic marketing"))
    assert "replaceable identity channel" not in rendered
    assert "acceptance criteria" not in rendered.lower()


def test_engineering_is_shown_when_there_was_engineering() -> None:
    built = {
        **SYNTHESIS,
        "branch": "office/task-1",
        "changed_files": ["app/main.py"],
        "tests": "passed",
    }
    rendered = to_markdown(document_for("final_report", built, title="A change"))
    assert "office/task-1" in rendered


def test_nothing_is_said_twice() -> None:
    """The reasoning belongs to the answer, not to the answer and the working."""
    rendered = to_markdown(document_for("synthesis", SYNTHESIS, title="Agentic marketing"))
    assert rendered.count("Discovery may shift towards assistants") == 1


def test_the_working_is_folded_on_screen() -> None:
    from app.documents import body_html

    html = body_html(document_for("synthesis", SYNTHESIS, title="Agentic marketing"))
    assert "<details" in html and "How it got there" in html
    assert html.index("The answer") < html.index("<details")


def test_a_payload_with_no_answer_still_reads() -> None:
    """A research report has findings rather than a recommendation."""
    document = document_for("research_report", {"key_findings": ["A thing"], "evidence": ["Why"]})
    assert document.sections, "it must still render something"
    assert "A thing" in to_markdown(document)


def test_a_deck_the_owner_asked_for_leads_the_document() -> None:
    """The owner's report: a presentation came out as a report with page breaks.

    The synthesis has fixed fields, so a deck built from them was always
    "The answer, Facts, Unknowns" whatever the task asked for.
    """
    document = document_for(
        "synthesis",
        {
            "recommendation": "Apply this quarter.",
            "facts": ["Meta published Muse on 8 September 2026."],
            "unknowns": ["No adoption metrics are published."],
            "deliverable_body": [
                {"heading": "The decision asked for", "points": ["Approve five prospects"]},
                {"heading": "The customer problem", "points": ["Merchants cannot see surfaces"]},
            ],
        },
        title="Muse readiness pilot",
    )
    headings = [section.heading for section in document.sections]
    assert headings[:2] == ["The decision asked for", "The customer problem"]
    # And nothing is lost: the report's own sections follow, as the working.
    assert "The answer" in headings
    assert all(section.supporting for section in document.sections[2:])


def test_a_slide_without_a_heading_or_points_is_not_a_slide() -> None:
    document = document_for(
        "synthesis",
        {
            "recommendation": "Apply.",
            "facts": ["One."],
            "deliverable_body": [
                {"heading": "Real", "points": ["Something"]},
                {"heading": "", "points": ["Orphaned"]},
                {"heading": "Empty", "points": []},
                "not a slide at all",
            ],
        },
    )
    assert [s.heading for s in document.sections][:1] == ["Real"]


def test_the_deck_is_not_also_listed_as_a_stray_field() -> None:
    """The body is the deliverable; it must not reappear under "Also recorded"."""
    document = document_for(
        "synthesis",
        {
            "recommendation": "Apply.",
            "facts": ["One."],
            "deliverable_body": [{"heading": "Only", "points": ["Once"]}],
        },
    )
    recorded = [s for s in document.sections if s.heading == "Also recorded"]
    assert not recorded or "slides" not in str(recorded[0].blocks).lower()


def test_slides_become_one_powerpoint_slide_each() -> None:
    from app.documents.office_files import to_pptx

    document = document_for(
        "synthesis",
        {
            "recommendation": "Apply.",
            "facts": ["One."],
            "deliverable_body": [
                {"heading": "First", "points": ["A", "B"]},
                {"heading": "Second", "points": ["C"]},
            ],
        },
        title="A deck",
    )
    deck = to_pptx(document)
    assert deck[:2] == b"PK"
    assert len(deck) > 10_000


def test_a_deck_written_under_the_earlier_field_name_is_still_read() -> None:
    """`slides` was the field for a few hours before every shape shared one.

    A run in flight when the name changed must still render as its deck.
    """
    document = document_for(
        "synthesis",
        {
            "recommendation": "Apply.",
            "facts": ["One."],
            "slides": [{"heading": "Written earlier", "points": ["Still a slide"]}],
        },
    )
    assert document.sections[0].heading == "Written earlier"


def test_an_answer_written_for_the_reader_leads_and_folds_the_working() -> None:
    """Q4: the answer, the options, what to do next, what is open; then the working."""
    payload = {
        "lead": "Three classes fit a 5-year-old near Lyon: Atelier A, B and C.",
        "deliverable_body": [
            {
                "heading": "The classes",
                "points": ["Atelier A: Saturdays 10:00, EUR 12, email to sign up"],
            }
        ],
        "next_steps": ["Email Atelier A for a trial lesson"],
        "not_checked": "B's prices are not published; ask when you call.",
        "facts": ["Atelier A takes children from 4"],
        "inferences": ["A suits a beginner"],
        "recommendation": "Start with Atelier A.",
        "unknowns": ["C's group size"],
    }
    document = document_for("synthesis", payload, "Art classes")
    headings = [(s.heading, s.supporting) for s in document.sections]
    assert headings[:4] == [
        ("The answer", False),
        ("The classes", False),
        ("What to do next", False),
        ("What we could not check", False),
    ]
    folded = {heading for heading, supporting in headings[4:] if supporting}
    assert {"Facts", "Inferences", "Recommendation", "Unknowns"} <= folded
    assert all(supporting for _, supporting in headings[4:])


def test_an_answer_from_before_reads_as_it_did() -> None:
    document = document_for(
        "synthesis", {"recommendation": "Start with A.", "facts": ["A exists"]}, "Old"
    )
    assert document.sections[0].heading == "The answer"
    assert "Start with A." in str(document.sections[0].blocks[0])
