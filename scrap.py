"""Download, extract, index and export disciplinary documents. See README.md."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from extraction import extract_pages, parse_records
from exports import atomic_write, export

LOG = logging.getLogger("research_pipeline")
BASE_URL = "https://memberbase.com/ncbelsDiscDoc/showimage.aspx?Doc_Id={doc_id}"
MAX_BYTES = 25 * 1024 * 1024
PARSER_VERSION = "1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(output: Path) -> sqlite3.Connection:
    output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(output / "research.sqlite3")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            sha256 TEXT PRIMARY KEY, raw_path TEXT NOT NULL, text_path TEXT NOT NULL,
            pages INTEGER NOT NULL, text TEXT NOT NULL, needs_ocr TEXT NOT NULL,
            processed_at TEXT NOT NULL, parser_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources (
            source TEXT PRIMARY KEY, doc_id INTEGER, sha256 TEXT REFERENCES documents(sha256),
            status TEXT NOT NULL, error TEXT NOT NULL, attempted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cases (
            sha256 TEXT NOT NULL REFERENCES documents(sha256), record_no INTEGER NOT NULL,
            data TEXT NOT NULL, PRIMARY KEY (sha256, record_no)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS document_search USING fts5(sha256 UNINDEXED, text);
    """)
    return db


def record_failure(db: sqlite3.Connection, source: str, doc_id: int | None, error: str) -> None:
    with db:
        db.execute("""INSERT INTO sources VALUES (?, ?, NULL, 'failed', ?, ?)
            ON CONFLICT(source) DO UPDATE SET status='failed', error=excluded.error,
            attempted_at=excluded.attempted_at""", (source, doc_id, error, now()))


def ingest(db: sqlite3.Connection, output: Path, source: str, content: bytes,
           doc_id: int | None = None, ocr: bool = False, tesseract: str | None = None) -> None:
    if len(content) > MAX_BYTES:
        raise ValueError(f"Document exceeds {MAX_BYTES // 1024 // 1024} MB limit")
    digest = hashlib.sha256(content).hexdigest()
    if content.lstrip().startswith(b"%PDF-"):
        # Retain original bytes even when subsequent PDF parsing fails.
        atomic_write(output / "raw" / f"{digest}.pdf", content)
    pages, needs_ocr = extract_pages(content, ocr, tesseract)
    # Do not lose previously recovered OCR text on a later run without --ocr.
    previous = db.execute("SELECT text, needs_ocr FROM documents WHERE sha256=?", (digest,)).fetchone()
    if previous:
        previous_pages = previous["text"].split("\n\f\n")
        previous_pending = set(json.loads(previous["needs_ocr"]))
        for number in list(needs_ocr):
            if number not in previous_pending and number <= len(previous_pages):
                pages[number - 1] = previous_pages[number - 1]
                needs_ocr.remove(number)
    records = parse_records(pages)
    for record in records:
        if needs_ocr:
            record["review_reasons"].append("Insufficient text on pages: " + ", ".join(map(str, needs_ocr)))
    text = "\n\f\n".join(pages)
    text_path = Path("text") / f"{digest}.txt"
    atomic_write(output / text_path, text.encode("utf-8"))
    with db:
        db.execute("""INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET text=excluded.text, needs_ocr=excluded.needs_ocr,
            processed_at=excluded.processed_at, parser_version=excluded.parser_version""",
            (digest, f"raw/{digest}.pdf", text_path.as_posix(), len(pages), text,
             json.dumps(needs_ocr), now(), PARSER_VERSION))
        db.execute("DELETE FROM cases WHERE sha256=?", (digest,))
        db.executemany("INSERT INTO cases VALUES (?, ?, ?)",
                       [(digest, i, json.dumps(r, ensure_ascii=False)) for i, r in enumerate(records, 1)])
        db.execute("DELETE FROM document_search WHERE sha256=?", (digest,))
        db.execute("INSERT INTO document_search VALUES (?, ?)", (digest, text))
        db.execute("""INSERT INTO sources VALUES (?, ?, ?, 'ok', '', ?)
            ON CONFLICT(source) DO UPDATE SET doc_id=excluded.doc_id, sha256=excluded.sha256,
            status='ok', error='', attempted_at=excluded.attempted_at""", (source, doc_id, digest, now()))
    LOG.info("Processed %s: %d pages, %d records%s", source, len(pages), len(records),
             "; needs OCR/review" if needs_ocr else "")


def session() -> requests.Session:
    client = requests.Session()
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET"}), respect_retry_after_header=True)
    client.mount("https://", HTTPAdapter(max_retries=retry))
    client.mount("http://", HTTPAdapter(max_retries=retry))
    client.headers.update({"User-Agent": "ResearchDocumentPipeline/1.0"})
    return client


def fetch_pdf(client: requests.Session, url: str) -> bytes:
    with client.get(url, timeout=(10, 60), stream=True) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("Download exceeded document size limit")
            chunks.append(chunk)
        content = b"".join(chunks)
    if not content:
        raise ValueError("Empty response")
    return content


def search(db: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    phrase = '"' + query.replace('"', '""') + '"'
    return [dict(row) for row in db.execute("""SELECT d.sha256, d.raw_path,
        snippet(document_search, 1, '[', ']', ' ... ', 30) AS excerpt,
        (SELECT group_concat(source, char(10)) FROM sources s
         WHERE s.sha256=d.sha256 AND s.status='ok') AS sources
        FROM document_search JOIN documents d ON d.sha256=document_search.sha256
        WHERE document_search MATCH ? AND EXISTS (
            SELECT 1 FROM sources s WHERE s.sha256=d.sha256 AND s.status='ok')
        ORDER BY rank LIMIT ?""", (phrase, limit))]


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("download", "import", "search"):
        command = commands.add_parser(name)
        command.add_argument("--output", type=Path, default=Path("output"))
        if name == "download":
            command.add_argument("--start-id", type=int, required=True)
            command.add_argument("--end-id", type=int, required=True)
            command.add_argument("--base-url", default=BASE_URL, help="URL containing {doc_id}")
            command.add_argument("--delay", type=float, default=1.0, help="Seconds between documents")
            command.add_argument("--refresh", action="store_true", help="Refetch successful sources")
        elif name == "import":
            command.add_argument("--input", type=Path, required=True, help="PDF file or directory")
        else:
            command.add_argument("query")
            command.add_argument("--limit", type=int, default=10)
        if name != "search":
            command.add_argument("--ocr", action="store_true", help="OCR pages with little extractable text")
            command.add_argument("--tesseract", help="Path to tesseract executable")
    return root


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.command == "download":
        if args.start_id < 1 or args.end_id < args.start_id or args.delay < 0:
            cli.error("Use positive IDs, end-id >= start-id, and a nonnegative delay")
        if "{doc_id}" not in args.base_url or not args.base_url.startswith(("https://", "http://")):
            cli.error("base-url must be an HTTP(S) URL containing {doc_id}")
    if args.command == "search" and (not args.query.strip() or args.limit < 1):
        cli.error("Provide a nonempty search phrase and a positive limit")
    if args.command == "search" and not (args.output / "research.sqlite3").is_file():
        cli.error("No index found; download or import documents first")
    files = []
    if args.command == "import":
        if not args.input.exists():
            cli.error("Input path does not exist")
        if args.input.is_dir():
            files = sorted(p for p in args.input.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"
                           and not p.resolve().is_relative_to(args.output.resolve()))
        elif args.input.suffix.lower() == ".pdf":
            files = [args.input]
        if not files:
            cli.error("No input PDFs found (output directory is excluded)")
    if getattr(args, "ocr", False):
        try:
            import pypdfium2  # noqa: F401
            import pytesseract  # noqa: F401
        except ImportError:
            cli.error("OCR requires: python -m pip install -r requirements-ocr.txt")
        if not shutil.which(args.tesseract or "tesseract"):
            cli.error("Tesseract not found; install it or pass --tesseract PATH")
    args.output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(args.output / "pipeline.log", encoding="utf-8")],
                        force=True)
    db = connect(args.output)
    try:
        if args.command == "search":
            print(json.dumps(search(db, args.query, args.limit), ensure_ascii=False, indent=2))
            return 0
        failures, processed, cached = 0, 0, 0
        with session() as client:
            items = [(str(p.resolve()), None, p) for p in files] if args.command == "import" else [
                (args.base_url.format(doc_id=i), i, None) for i in range(args.start_id, args.end_id + 1)]
            for source, doc_id, path in items:
                previous = db.execute("SELECT * FROM sources WHERE source=?", (source,)).fetchone()
                if args.command == "download" and previous and previous["status"] == "ok" and not args.refresh:
                    raw = args.output / "raw" / f"{previous['sha256']}.pdf"
                    if raw.is_file():
                        path = raw  # Reprocess cached bytes, including when enabling OCR.
                        cached += 1
                try:
                    if path is None:
                        time.sleep(args.delay)
                        content = fetch_pdf(client, source)
                    else:
                        if path.stat().st_size > MAX_BYTES:
                            raise ValueError("Local PDF exceeded document size limit")
                        content = path.read_bytes()
                    ingest(db, args.output, source, content, doc_id, args.ocr, args.tesseract)
                    processed += 1
                except Exception as error:
                    # A malformed document must not discard the rest of the batch.
                    failures += 1
                    record_failure(db, source, doc_id, f"{type(error).__name__}: {error}")
                    LOG.error("Failed %s: %s", source, error)
        rows, total_failed = export(db, args.output)
        LOG.info("Finished: %d processed, %d cached downloads reused, %d failed this run; "
                 "%d exported rows, %d failed sources in manifest", processed, cached, failures, rows, total_failed)
        LOG.info("All extracted records require source review. See Review Reasons and Needs OCR Pages in the exports.")
        return 1 if failures else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
