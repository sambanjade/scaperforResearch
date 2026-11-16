"""Functional checks using synthetic PDFs; no website or private records required."""
import csv
import io
import json
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET
import zipfile

import pytest
import requests
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter

import scrap
from extraction import extract_pages, normalize_date, parse_records
from exports import export, spreadsheet_safe, xlsx_bytes

CASE_ONE = """CASE NO. V2024-001
Alex Example, PE [012345]
Raleigh, NC
Settlement Date: November 13, 2024
VIOLATION: Failed to maintain accurate engineering records.
BOARD ACTION: Reprimand and a civil penalty of $500.
"""
CASE_TWO = """CASE NO. V2024-002
Respondent: Jordan Sample, PLS
Decision Date: 12/01/2024
VIOLATIONS: Practiced without a current license.
PENALTY: Suspension for six months.
"""


def pdf_bytes(*pages):
    stream = io.BytesIO()
    document = canvas.Canvas(stream, pagesize=(612, 792), invariant=1)
    for page in pages:
        text = document.beginText(50, 740)
        text.setFont("Helvetica", 11)
        for line in page.splitlines():
            text.textLine(line)
        document.drawText(text)
        document.showPage()
    document.save()
    return stream.getvalue()


@pytest.fixture
def store(tmp_path):
    db = scrap.connect(tmp_path)
    yield db, tmp_path
    db.close()


def test_two_case_extraction_keeps_fields_and_page_provenance_separate():
    records = parse_records([CASE_ONE, CASE_TWO])
    assert len(records) == 2
    assert records[0]["case"] == "V2024-001"
    assert records[0]["party"] == "Alex Example, PE [012345]"
    assert records[0]["settle_date"] == "2024-11-13"
    assert records[0]["penalty"] == "Reprimand and a civil penalty of $500."
    assert records[1]["party"] == "Jordan Sample, PLS"
    assert records[1]["settle_date"] == ""
    assert records[1]["decision_date"] == "2024-12-01"
    assert records[1]["page_start"] == 2
    assert records[0]["page_end"] == 1
    assert all(record["review_required"] for record in records)
    assert "VIOLATION:" in records[0]["evidence"]["violation"]


def test_unknown_and_invalid_fields_are_not_invented():
    record = parse_records(["Ordinary document, signed 01/02/2024."])[0]
    assert record["case"] == record["party"] == record["settle_date"] == ""
    record = parse_records(["Settlement Date: 02/31/2024\nEffective Date: 01/02/2024"])[0]
    assert record["settle_date"] == record["decision_date"] == ""
    assert any("Unrecognized" in item for item in record["review_reasons"])
    assert normalize_date("November 13th, 2024") == "2024-11-13"


def test_letter_caption_does_not_turn_potential_penalties_into_actual_penalties():
    record = parse_records(["""January 2, 2024
RE: Case No. V2024-003
Example Mapping Services, LLC
Dear Sir or Madam:
This letter is not a final determination. A court may impose a penalty.
"""])[0]
    assert record["case"] == "V2024-003"
    assert record["party"] == "Example Mapping Services, LLC"
    assert record["violation"] == record["penalty"] == record["settle_date"] == ""


def test_repeated_headers_do_not_duplicate_case_and_conflicts_need_review():
    records = parse_records([CASE_ONE, "CASE NO. V2024-001\nPenalty: Different action."])
    assert len(records) == 1
    assert records[0]["penalty"] == "Reprimand and a civil penalty of $500."
    assert any("Multiple candidates for penalty" in reason for reason in records[0]["review_reasons"])


def test_pdf_extraction_blank_page_and_bad_or_encrypted_pdf():
    pages, needs_ocr = extract_pages(pdf_bytes(CASE_ONE, ""))
    assert "V2024-001" in pages[0]
    assert needs_ocr == [2]
    with pytest.raises(ValueError, match="Not a PDF"):
        extract_pages(b"<html>not a PDF</html>")
    with pytest.raises(Exception):
        extract_pages(b"%PDF-broken")
    writer = PdfWriter()
    writer.add_page(PdfReader(io.BytesIO(pdf_bytes(CASE_ONE))).pages[0])
    writer.encrypt("secret")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(ValueError, match="Password-protected"):
        extract_pages(encrypted.getvalue())


def test_idempotence_duplicate_content_and_search(store):
    db, output = store
    content = pdf_bytes(CASE_ONE)
    scrap.ingest(db, output, "https://example.test/1", content, 1)
    scrap.ingest(db, output, "https://example.test/1", content, 1)
    scrap.ingest(db, output, "https://example.test/2", content, 2)
    assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM sources").fetchone()[0] == 2
    results = scrap.search(db, "accurate engineering", 10)
    assert len(results) == 1
    assert "https://example.test/2" in results[0]["sources"]
    assert len(list((output / "raw").glob("*.pdf"))) == 1
    assert scrap.search(db, '" OR *', 10) == []


def test_updated_source_does_not_return_old_document(store):
    db, output = store
    scrap.ingest(db, output, "source", pdf_bytes(CASE_ONE))
    scrap.ingest(db, output, "source", pdf_bytes(CASE_TWO))
    assert scrap.search(db, "accurate engineering", 10) == []
    assert len(scrap.search(db, "Suspension", 10)) == 1
    assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 2


def test_failed_refresh_is_visible_and_does_not_export_stale_success(store):
    db, output = store
    scrap.ingest(db, output, "source", pdf_bytes(CASE_ONE))
    scrap.record_failure(db, "source", 1, "HTTP 500")
    assert export(db, output) == (0, 1)
    assert json.loads((output / "Case_Details.json").read_text()) == []
    assert "HTTP 500" in (output / "sources.json").read_text()
    assert scrap.search(db, "engineering", 10) == []
    assert (output / "Case_Details.xlsx").is_file()


def test_native_rerun_keeps_successful_prior_ocr_text(store, monkeypatch):
    db, output = store
    content = pdf_bytes("")
    monkeypatch.setattr(scrap, "extract_pages", lambda *args: ([CASE_ONE], []))
    scrap.ingest(db, output, "source", content, ocr=True)
    monkeypatch.setattr(scrap, "extract_pages", lambda *args: ([""], [1]))
    scrap.ingest(db, output, "source", content)
    assert len(scrap.search(db, "engineering", 10)) == 1
    assert db.execute("SELECT needs_ocr FROM documents").fetchone()[0] == "[]"


def test_exports_preserve_evidence_and_csv_rows(store):
    db, output = store
    scrap.ingest(db, output, "source", pdf_bytes(CASE_ONE))
    assert export(db, output) == (1, 0)
    rows = list(csv.DictReader((output / "Case_Details.csv").open(encoding="utf-8-sig", newline="")))
    assert rows[0]["Party"] == "Alex Example, PE [012345]"
    assert rows[0]["Review Required"] == "True"
    evidence = json.loads(rows[0]["Evidence"])
    assert "500" in evidence["penalty"]
    assert (output / rows[0]["Raw PDF"]).is_file()
    assert (output / rows[0]["Text File"]).is_file()


def test_xlsx_package_literal_cells_xml_and_excel_length_limits():
    payload = xlsx_bytes(["A", "B"], [{"A": "=HYPERLINK(\"bad\")", "B": "x\x00\f&<>" + "y" * 40000}])
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            ET.fromstring(archive.read(name))
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        assert root.findall(".//s:f", ns) == []
        texts = root.findall(".//s:t", ns)
        assert texts[2].text.startswith("=HYPERLINK")
        assert len(texts[3].text) == 32767
    assert spreadsheet_safe("  =2+2").startswith("'")
    assert spreadsheet_safe(None) == ""


def response_client(content=b"", status_error=None):
    client, response = MagicMock(), MagicMock()
    client.get.return_value.__enter__.return_value = response
    response.raise_for_status.side_effect = status_error
    response.iter_content.return_value = [content]
    return client


def test_download_timeout_status_empty_and_size_checks(monkeypatch):
    client = response_client(b"pdf bytes")
    assert scrap.fetch_pdf(client, "https://example.test") == b"pdf bytes"
    assert client.get.call_args.kwargs == {"timeout": (10, 60), "stream": True}
    with pytest.raises(requests.HTTPError):
        scrap.fetch_pdf(response_client(status_error=requests.HTTPError("404")), "url")
    with pytest.raises(ValueError, match="Empty"):
        scrap.fetch_pdf(response_client(), "url")
    monkeypatch.setattr(scrap, "MAX_BYTES", 2)
    with pytest.raises(ValueError, match="size limit"):
        scrap.fetch_pdf(response_client(b"123"), "url")
    client = MagicMock()
    client.get.side_effect = requests.Timeout("timed out")
    with pytest.raises(requests.Timeout):
        scrap.fetch_pdf(client, "url")
    with scrap.session() as client:
        retry = client.get_adapter("https://").max_retries
        assert retry.total == 3 and 429 in retry.status_forcelist


def test_cli_import_continues_after_bad_pdf_and_writes_manifest(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    (source / "good.PDF").write_bytes(pdf_bytes(CASE_ONE, CASE_TWO))
    (source / "bad.pdf").write_bytes(b"HTML error")
    output = tmp_path / "output"
    assert scrap.main(["import", "--input", str(source), "--output", str(output)]) == 1
    assert len(json.loads((output / "Case_Details.json").read_text())) == 2
    sources = json.loads((output / "sources.json").read_text())
    assert sorted(s["status"] for s in sources) == ["failed", "ok"]


def test_cli_download_reuses_cache_and_refreshes(monkeypatch, tmp_path):
    fetch = MagicMock(return_value=pdf_bytes(CASE_ONE))
    monkeypatch.setattr(scrap, "fetch_pdf", fetch)
    args = ["download", "--start-id", "185", "--end-id", "185", "--delay", "0", "--output", str(tmp_path)]
    assert scrap.main(args) == 0
    assert scrap.main(args) == 0
    assert fetch.call_count == 1
    assert scrap.main(args + ["--refresh"]) == 0
    assert fetch.call_count == 2


def test_cli_invalid_arguments_do_not_start_work(tmp_path):
    with pytest.raises(SystemExit) as exc:
        scrap.main(["download", "--start-id", "200", "--end-id", "185"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        scrap.main(["search", "test", "--output", str(tmp_path)])


def test_ocr_scanned_pdf_when_tesseract_is_available():
    import shutil
    tesseract = shutil.which("tesseract")
    windows_path = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")
    if not tesseract and windows_path.exists():
        tesseract = str(windows_path)
    if not tesseract:
        pytest.skip("Tesseract is not installed")
    pypdfium2 = pytest.importorskip("pypdfium2")
    pytest.importorskip("pytesseract")
    from reportlab.lib.utils import ImageReader
    document = pypdfium2.PdfDocument(pdf_bytes(CASE_ONE))
    page = document[0]
    bitmap = page.render(scale=2)
    image = bitmap.to_pil()
    try:
        stream = io.BytesIO()
        scanned = canvas.Canvas(stream, pagesize=(612, 792))
        scanned.drawImage(ImageReader(image), 0, 0, width=612, height=792)
        scanned.save()
        content = stream.getvalue()
        assert extract_pages(content)[1] == [1]
        pages, pending = extract_pages(content, ocr=True, tesseract=tesseract)
        assert pending == []
        record = parse_records(pages)[0]
        assert record["case"] == "V2024-001"
        assert "500" in record["penalty"]
    finally:
        image.close()
        bitmap.close()
        page.close()
        document.close()
