"""Generate fictional case PDFs and run the complete offline pipeline."""
from pathlib import Path
import argparse
from reportlab.pdfgen import canvas
import scrap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("demo_output"))
    args = parser.parse_args()
    source = args.output / "sample_inputs"
    source.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(source / "fictional_cases.pdf"), invariant=1)
    samples = [
        ["FICTIONAL TEST DATA - NOT A REAL DISCIPLINARY RECORD", "CASE NO. V2024-001",
         "Alex Example, PE [012345]", "Settlement Date: November 13, 2024",
         "VIOLATION: Failed to maintain accurate engineering records.",
         "BOARD ACTION: Reprimand and a civil penalty of $500."],
        ["FICTIONAL TEST DATA - NOT A REAL DISCIPLINARY RECORD", "CASE NO. V2024-002",
         "Respondent: Jordan Sample, PLS", "Decision Date: 12/01/2024",
         "VIOLATION: Practiced without a current license.", "PENALTY: Suspension for six months."],
    ]
    for lines in samples:
        text = pdf.beginText(45, 740)
        text.setFont("Helvetica", 10)
        for line in lines:
            text.textLine(line)
        pdf.drawText(text)
        pdf.showPage()
    pdf.save()
    output = args.output / "results"
    code = scrap.main(["import", "--input", str(source), "--output", str(output)])
    if code == 0:
        scrap.main(["search", "civil penalty", "--output", str(output)])
        print(f"Fictional demo workbook: {(output / 'Case_Details.xlsx').resolve()}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
