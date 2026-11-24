"""Make low-resolution, heavily compressed copies of labelled documents, to produce extractor mistakes on purpose.

    python degrade.py

Writes data/degraded/<name>.jpg and copies <name>.json (the answers) beside it. A damaged copy has the same ground
truth as its original, so `python eval.py --labelled data/degraded` scores the extractor on the damaged copies.
Only single-page invoices are copied: more pages cost more tokens, and a non-invoice has no fields to get wrong.
"""
import json
import shutil
from pathlib import Path

import pymupdf

HERE = Path(__file__).parent
SOURCE, TARGET = HERE / "data" / "labelled", HERE / "data" / "degraded"
DPI = 42      # a PDF page is rendered at this resolution (the extractor itself uses 150)
SHRINK = 2    # an image is made smaller: width and height are divided by 2 ** SHRINK
QUALITY = 15  # JPEG quality out of 100; ordinary photos are 75 to 90
DOCUMENTS = {".pdf", ".jpg", ".jpeg", ".png"}


def degrade(path):
    """The document as the bytes of a small, blurry, heavily compressed JPEG, or None if it should be left out."""
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as pdf:
            if len(pdf) != 1:
                return None
            pix = pdf[0].get_pixmap(dpi=DPI)
    else:
        pix = pymupdf.Pixmap(str(path))
        pix.shrink(SHRINK)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)  # JPEG has no transparency
    return pix.tobytes("jpeg", jpg_quality=QUALITY)


def main():
    TARGET.mkdir(parents=True, exist_ok=True)
    made = []
    for answers in sorted(SOURCE.glob("*.json")):
        if not json.loads(answers.read_text(encoding="utf-8"))["is_invoice"]:
            continue
        doc = next(p for p in SOURCE.iterdir() if p.stem == answers.stem and p.suffix.lower() in DOCUMENTS)
        data = degrade(doc)
        if data is None:
            continue
        (TARGET / f"{doc.stem}.jpg").write_bytes(data)
        shutil.copy(answers, TARGET / answers.name)
        made.append(doc.stem)
    print(f"wrote {len(made)} damaged copies to {TARGET}: {', '.join(made)}")


if __name__ == "__main__":
    main()
