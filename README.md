# Research document pipeline

Collect disciplinary PDFs, retain originals, extract case fields, and search their
text. This replaces the original placeholder-only scraper.

## Quick start (PowerShell)

Requires Python 3.10+ with SQLite FTS5 (included in standard Windows Python).
From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scrap.py download --start-id 185 --end-id 200
.\.venv\Scripts\python.exe scrap.py search "civil penalty"
```

The inclusive range above is the original example, **not a verified list of
existing PDFs**. Missing IDs are recorded as failures. To process saved PDFs:

```powershell
.\.venv\Scripts\python.exe scrap.py import --input 'C:\path\to\saved-pdfs'
```

Input can be one PDF or a directory, recursively including `.PDF` files. The output
directory is excluded from recursive input discovery. Each command accepts
`--output PATH` (default: `output` in your current directory). Use the same path for
later searches. `download --base-url` accepts an HTTP(S) URL containing `{doc_id}`.
The default is the original `https://memberbase.com/ncbelsDiscDoc/showimage.aspx?Doc_Id={doc_id}`.

## Scanned PDFs / OCR

Install the external Tesseract OCR executable and optional Python packages:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt
.\.venv\Scripts\python.exe scrap.py import --input 'C:\path\to\saved-pdfs' --ocr --tesseract 'C:\Program Files\Tesseract-OCR\tesseract.exe'
```

Omit `--tesseract` when it is on PATH. `--ocr` also works with `download`. Pages with
fewer than 40 extracted word characters trigger OCR or are flagged in **Needs OCR
Pages**. Blank pages can trigger this flag; a scan with a text-rich footer may evade
it. OCR uses English and a 90-second timeout per page. Always review source files.

## Outputs

| File | Purpose |
| --- | --- |
| `raw/<sha256>.pdf` | Original PDF bytes |
| `text/<sha256>.txt` | Extracted text with page boundaries |
| `research.sqlite3` | Sources, case records, SQLite FTS5 document index |
| `Case_Details.xlsx` | Excel workbook with case fields and review information |
| `Case_Details.csv` | CSV export with potential formulas escaped |
| `Case_Details.json` | Full field values without Excel cell-length truncation |
| `sources.json` | Success/failure status and error per source |
| `pipeline.log` | Processing log and batch summary |

Raw/text paths are relative to the output directory. The standard-library XLSX
writer emits literal string cells; pandas, openpyxl, and Excel are not required.
Excel cells are limited to 32,767 characters; full values remain in JSON and full
document text remains in the text files/database.

## Extraction scope and review

Supports case/file captions (`CASE NO. V2024-001`, `RE: Case No. V2024-003`), multiple
case summaries per PDF, explicitly labelled respondent/violation/penalty fields,
and respondent names directly below captions with credentials or company suffixes.
Repeated consecutive case captions are treated as page headers.

Dates accept US numeric, ISO, and English month-name formats. Explicit settlement
dates and explicit decision/order dates use **separate columns**. Letter dates and
effective dates are not assumed to be settlement dates. Source `Doc_Id` and actual
case number are separate. Penalty wording is retained in context rather than
automatically summing dollar amounts.

Every row has **Review Required = True**, reasons, page range, and field evidence.
Missing values remain blank. Narrative orders, unusual captions, multi-column
layouts, tables, section continuations, and poor OCR may need manual review or new
format-specific rules in `extraction.py`. Labelled sections stop at the next known
heading, blank line, or page boundary. Full text remains searchable when fields
cannot be extracted. Document finality is not automatically classified: an
allegation, notice letter, or potential penalty must not be treated as a final finding.

## Reliability and reruns

- SHA-256 deduplicates stored PDF content and search entries. Distinct source
  URLs/files remain separate manifest entries; exports have one row per source
  and case. Group by SHA256 + Record to identify duplicate-source rows.
- Successful downloads reuse cached PDFs, reprocessing them for new rules/OCR.
  Use `--refresh` to check changed source documents. Failed sources are retried
  when included in another run. Native reruns preserve prior successful OCR text.
- GET requests use connect/read timeouts, bounded connection/status retries,
  backoff, and a one-second delay between documents (`--delay` changes this).
  Interrupted streamed downloads are recorded for a later rerun.
- Empty/non-PDF responses, unreadable/encrypted documents, and oversized files are
  failures. Limits: 25 MiB per file and 200 pages per PDF. These limits are not a
  complete sandbox for hostile PDFs.
- One failed document does not discard a batch. Exit codes: `1` for document
  failures in this run, `2` for invalid arguments, otherwise `0`. **Exit 0 means
  processing finished, not that extracted values were verified.**
- Exports cover all currently successful sources in the database, including earlier
  runs. A failed refresh hides that source's old records from current export/search
  until it succeeds. Old PDF versions stay stored, but are searchable only while
  linked to a successful source. Failed PDFs with a PDF signature are retained too.
- Run one writer process per output directory. Choose a new output directory for
  an independent research batch.

Search is literal phrase full-text search with ranked snippets, not vector search:

```powershell
.\.venv\Scripts\python.exe scrap.py search "license suspension" --limit 20
```

## Tests and offline demo

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe demo.py
```

The demo generates explicitly fictional documents, imports two records, exports
Excel/CSV/JSON, and searches the index. Its workbook is
`demo_output/results/Case_Details.xlsx`; these are not real research findings.
Tests cover field boundaries, missing values, actual OCR, duplicate content, changed
sources, failure recovery, exports, caching, and mocked HTTP failures. The OCR test
skips when its optional software is unavailable.

Development validation: **16 tests passed, including actual Tesseract OCR**, and the
offline demo completed. Live IDs 185–200 could not be verified: shell networking is
restricted and the web fetcher could not retrieve them. The downloader needs a live
check on a network that reaches the source. Public ID 76978 was readable through
the web fetcher, but its bytes were not downloaded as a local fixture.

## Interview explanation

```text
Extract: HTTP download or saved PDF import
  -> Retain: original PDFs + source manifest
  -> Transform: native text / OCR + field extraction + review flags
  -> Load: SQLite records + full-text index + Excel/CSV/JSON
  -> Retrieve: phrase search with snippets and source references
```

Describe this as document ingestion and ETL with retained raw sources. Retaining
raw PDFs enables reprocessing; it does not by itself establish warehouse-based
ELT. Distinguish the original scraper prototype from these new capabilities and
local validation from live-source testing.
