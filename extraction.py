"""Conservative case extraction: unknown fields stay blank, evidence stays visible."""
from __future__ import annotations

from datetime import datetime
import io
import re
import unicodedata
from pypdf import PdfReader

MAX_PAGES = 200
FIELDS = ("case", "party", "settle_date", "decision_date", "violation", "penalty")
CASE_RE = re.compile(
    r"(?im)^[ \t]*(?:RE:[ \t]*)?(?:case[ \t]*(?:nos?\.?|numbers?|#)|file[ \t]*(?:no\.?|number|#))"
    r"[ \t]*[:.\-]?[ \t]*(?P<case>[A-Z]?\d[\w./\-]*(?:[ \t]*(?:,|&|and)[ \t]*[A-Z]?\d[\w./\-]*)*)"
)
LABEL_RE = re.compile(
    r"(?im)^[ \t]*(?P<label>party|respondents?|licensee|name|settle(?:ment)? date|"
    r"decision date|order date|date of order|effective date|violations?|penalt(?:y|ies)|"
    r"board action|disciplinary action|sanctions?|findings of fact|conclusions of law|"
    r"consent order|decision and order|order)[ \t]*:[ \t]*"
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\x00", "")
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines())


def normalize_date(value: str) -> str:
    value = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", value.strip(), flags=re.I)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def parse_records(pages: list[str]) -> list[dict]:
    text = "\n\f\n".join(normalize(page) for page in pages)
    starts = []
    for match in CASE_RE.finditer(text):
        # Repeated consecutive case captions are page headers, not new cases.
        if not starts or match.group("case").strip() != starts[-1].group("case").strip():
            starts.append(match)
    spans = [(m.start(), starts[i + 1].start() if i + 1 < len(starts) else len(text),
              m.group("case").strip(), m.end()) for i, m in enumerate(starts)]
    if not spans:
        spans = [(0, len(text), "", 0)]
    records = []
    for start, end, case, caption_end in spans:
        block = text[start:end]
        record = dict.fromkeys(FIELDS, "")
        record.update(case=case, evidence={}, review_required=True, review_reasons=[],
                      page_start=text[:start].count("\f") + 1,
                      page_end=text[:start + len(block.rstrip())].count("\f") + 1)
        if case:
            record["evidence"]["case"] = text[start:caption_end].strip()
        labels = list(LABEL_RE.finditer(block))
        for i, label in enumerate(labels):
            stop = labels[i + 1].start() if i + 1 < len(labels) else len(block)
            value = block[label.end():stop].strip()
            value = re.split(r"\n\s*\n|\f", value, maxsplit=1)[0].strip()
            key = label.group("label").lower()
            if key in ("party", "respondent", "respondents", "licensee", "name"):
                field = "party"
            elif key in ("settle date", "settlement date"):
                field = "settle_date"
            elif key in ("decision date", "order date", "date of order"):
                field = "decision_date"
            elif key in ("violation", "violations"):
                field = "violation"
            elif key in ("penalty", "penalties", "board action", "disciplinary action", "sanction", "sanctions"):
                field = "penalty"
            else:
                continue
            if field in ("party", "settle_date", "decision_date"):
                value = value.splitlines()[0] if value else ""
            if not value:
                continue
            evidence = label.group(0).strip() + " " + value
            if field.endswith("date"):
                parsed = normalize_date(value)
                if not parsed:
                    record["review_reasons"].append(f"Unrecognized {field}: {value}")
                value = parsed
            if record[field] and value and record[field] != value:
                record["review_reasons"].append(f"Multiple candidates for {field}; first retained")
            elif value:
                record[field] = value
                record["evidence"][field] = evidence
        # NCBELS summaries put a name with a credential below the case caption.
        if case and not record["party"]:
            remainder = text[caption_end:end].lstrip(" \t\r\n")
            line = remainder.splitlines()[0].strip() if remainder else ""
            if (line and not LABEL_RE.match(line) and not CASE_RE.match(line)
                    and re.search(r"\b(?:P\.?E\.?|P\.?L\.?S\.?|Non[- ]licensed|LLC|Inc|Corporation|LLP)\b|\[", line, re.I)):
                record["party"] = line
                record["evidence"]["party"] = line
        missing = [field for field in ("case", "party", "violation", "penalty") if not record[field]]
        if missing:
            record["review_reasons"].append("Missing fields: " + ", ".join(missing))
        if not record["settle_date"]:
            record["review_reasons"].append("Settlement date not explicitly identified")
        record["review_reasons"].append("Rule-based extraction; verify against source document")
        records.append(record)
    return records


def extract_pages(content: bytes, ocr: bool = False, tesseract: str | None = None) -> tuple[list[str], list[int]]:
    if not content.lstrip().startswith(b"%PDF-"):
        raise ValueError("Not a PDF: missing PDF signature")
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("Password-protected PDF cannot be processed")
    if not 0 < len(reader.pages) <= MAX_PAGES:
        raise ValueError(f"PDF must have between 1 and {MAX_PAGES} pages")
    pages, needs_ocr = [], []
    renderer = None
    try:
        for number, page in enumerate(reader.pages):
            text = normalize(page.extract_text() or "")
            if len(re.sub(r"\W", "", text)) < 40:
                if ocr:
                    import pypdfium2
                    import pytesseract
                    if tesseract:
                        pytesseract.pytesseract.tesseract_cmd = tesseract
                    if renderer is None:
                        renderer = pypdfium2.PdfDocument(content)
                    rendered_page = renderer[number]
                    try:
                        bitmap = rendered_page.render(scale=2.5)
                        try:
                            image = bitmap.to_pil()
                            try:
                                recognized = normalize(pytesseract.image_to_string(image, timeout=90))
                            finally:
                                image.close()
                        finally:
                            bitmap.close()
                    finally:
                        rendered_page.close()
                    if len(re.sub(r"\W", "", recognized)) >= 40:
                        text = recognized
                    else:
                        needs_ocr.append(number + 1)
                else:
                    needs_ocr.append(number + 1)
            pages.append(text)
    finally:
        if renderer is not None:
            renderer.close()
    return pages, needs_ocr
