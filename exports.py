"""CSV, JSON and a one-sheet XLSX export with literal string cells."""
import csv
import io
import json
import re
from pathlib import Path
import tempfile
from xml.etree import ElementTree as ET
import zipfile


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def spreadsheet_safe(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def xlsx_bytes(headers: list[str], rows: list[dict]) -> bytes:
    """Small standards-based XLSX writer; avoids an Excel/pandas dependency."""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ET.register_namespace("", ns)
    sheet = ET.Element(f"{{{ns}}}worksheet")
    data = ET.SubElement(sheet, f"{{{ns}}}sheetData")
    for row_no, values in enumerate([headers] + [[row.get(h, "") for h in headers] for row in rows], 1):
        row_node = ET.SubElement(data, f"{{{ns}}}row", r=str(row_no))
        for column, value in enumerate(values):
            letters, index = "", column + 1
            while index:
                index, remainder = divmod(index - 1, 26)
                letters = chr(65 + remainder) + letters
            cell = ET.SubElement(row_node, f"{{{ns}}}c", r=f"{letters}{row_no}", t="inlineStr")
            inline = ET.SubElement(cell, f"{{{ns}}}is")
            node = ET.SubElement(inline, f"{{{ns}}}t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
            cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", "" if value is None else str(value))
            node.text = cleaned[:32767]
    files = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Case Details" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": ET.tostring(sheet, encoding="utf-8", xml_declaration=True),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return buffer.getvalue()


def export(db, output: Path) -> tuple[int, int]:
    headers = ["Case", "Party", "Settle Date", "Decision Date", "Violation", "Penalty", "Source", "Doc ID", "SHA256", "Record", "Page Start", "Page End", "Raw PDF", "Text File", "Needs OCR Pages", "Review Required", "Review Reasons", "Evidence", "Processed At", "Parser Version"]
    rows = []
    for item in db.execute("""SELECT s.source, s.doc_id, d.*, c.record_no, c.data
        FROM sources s JOIN documents d USING (sha256) JOIN cases c USING (sha256)
        WHERE s.status='ok' ORDER BY s.source, c.record_no"""):
        record = json.loads(item["data"])
        values = [record["case"], record["party"], record["settle_date"], record["decision_date"],
                  record["violation"], record["penalty"], item["source"], item["doc_id"], item["sha256"],
                  item["record_no"], record["page_start"], record["page_end"], item["raw_path"],
                  item["text_path"], item["needs_ocr"], True, "; ".join(record["review_reasons"]),
                  json.dumps(record["evidence"], ensure_ascii=False), item["processed_at"], item["parser_version"]]
        rows.append(dict(zip(headers, values)))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows([[spreadsheet_safe(row.get(h)) for h in headers] for row in rows])
    atomic_write(output / "Case_Details.csv", stream.getvalue().encode("utf-8-sig"))
    atomic_write(output / "Case_Details.json", json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"))
    atomic_write(output / "Case_Details.xlsx", xlsx_bytes(headers, rows))
    sources = [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY source")]
    atomic_write(output / "sources.json", json.dumps(sources, indent=2).encode("utf-8"))
    return len(rows), sum(source["status"] == "failed" for source in sources)
