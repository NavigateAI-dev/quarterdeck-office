"""Charts drawn from a document's own tables."""

import io
import zipfile

from app.deliverable import REGISTERED
from app.documents import document_for, to_html
from app.documents.charts import chart_of, charts_in, number, png, svg
from app.documents.model import Document, Section, Table
from app.documents.office_files import to_docx
from app.schemas.agent_output import BodyBlock


def test_numbers_are_read_as_people_write_them() -> None:
    assert number("1,234") == 1234
    assert number("12.5%") == 12.5
    assert number("€3.4m") == 3_400_000
    assert number("-7") == -7
    assert number("about twelve") is None
    assert number("") is None


def test_periods_make_a_line_and_names_make_bars() -> None:
    by_year = Table(
        ["Year", "Revenue (EUR k)"], [["2023", "120"], ["2024", "150"], ["2025", "190"]]
    )
    line = chart_of(by_year, "Revenue")
    assert line is not None and line.kind == "line" and line.series[0][1] == [120, 150, 190]
    by_rival = Table(["Rival", "Price (EUR)", "Seats"], [["Acme", "49", "10"], ["Brix", "39", "5"]])
    bars = chart_of(by_rival)
    assert (
        bars is not None
        and bars.kind == "bar"
        and [s[0] for s in bars.series]
        == [
            "Price (EUR)",
            "Seats",
        ]
    )


def test_a_table_of_words_is_not_drawn() -> None:
    assert chart_of(Table(["Vendor", "Notes"], [["A", "good"], ["B", "fine"]])) is None
    assert chart_of(Table(["Year", "Revenue"], [["2024", "150"]])) is None, "one point is no chart"


def test_labels_cannot_become_markup() -> None:
    table = Table(["Name", "Value"], [["<script>x</script>", "1"], ["b", "2"]])
    chart = chart_of(table, 'Title "&" <b>')
    assert chart is not None
    drawn = svg(chart)
    assert "<script>" not in drawn and "&lt;script&gt;" in drawn
    assert "&lt;b&gt;" in drawn


def test_a_chart_is_a_picture_for_slack_and_word() -> None:
    chart = chart_of(Table(["Year", "Users"], [["2024", "10"], ["2025", "30"], ["2026", "45"]]))
    assert chart is not None
    assert png(chart)[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_dashboard_block_carries_its_numbers_into_the_page_and_word() -> None:
    payload = {
        "lead": "Sign-ups tripled.",
        "facts": ["Counted from the admin export."],
        "recommendation": "Keep the campaign.",
        "deliverable_body": [
            {
                "heading": "Sign-ups by quarter",
                "points": ["Up every quarter."],
                "table": {
                    "columns": ["Quarter", "Sign-ups"],
                    "rows": [["Q1 2025", "100"], ["Q2 2025", "180"], ["Q3 2025", "300"]],
                },
            }
        ],
    }
    document = document_for("synthesis", payload | {"deliverable": {"shape": "dashboard"}})
    assert [chart.title for chart in charts_in(document, answer_only=False)] == [
        "Sign-ups by quarter"
    ]
    assert '<svg class="chart"' in to_html(document)
    word = zipfile.ZipFile(io.BytesIO(to_docx(document)))
    assert any(name.startswith("word/media/") for name in word.namelist())


def test_a_block_may_carry_a_table_and_the_dashboard_is_offered() -> None:
    block = BodyBlock(
        heading="Prices",
        points=["Acme is dearest."],
        table={"columns": ["Rival", "Price"], "rows": [["Acme", "49"]]},
    )
    assert block.table is not None and block.table.columns == ["Rival", "Price"]
    assert BodyBlock(heading="No numbers", points=["Fine."]).table is None
    assert REGISTERED["dashboard"].suits == "html" and REGISTERED["dashboard"].leads


def test_only_the_answers_tables_are_drawn_for_slack() -> None:
    working = Table(["Step", "Calls"], [["a", "1"], ["b", "2"]])
    answer = Table(["Year", "Sales"], [["2024", "1"], ["2025", "2"]])
    document = Document(
        "t",
        sections=[
            Section("Answer", [answer]),
            Section("How", [working], supporting=True),
        ],
    )
    assert [c.title for c in charts_in(document)] == ["Answer"]
