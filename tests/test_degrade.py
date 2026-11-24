import json

import pymupdf

import degrade
from test_extract import make_pdf


def folders(tmp_path, monkeypatch):
    source, target = tmp_path / "labelled", tmp_path / "degraded"
    source.mkdir()
    monkeypatch.setattr(degrade, "SOURCE", source)
    monkeypatch.setattr(degrade, "TARGET", target)
    return source, target


def test_single_page_invoices_are_copied_with_their_answers(tmp_path, monkeypatch):
    source, target = folders(tmp_path, monkeypatch)
    make_pdf(source / "A.pdf", 1)
    make_pdf(source / "B.pdf", 2)  # two pages: left out, it would cost twice the tokens
    make_pdf(source / "C.pdf", 1)  # not an invoice: no fields to get wrong
    for name, is_invoice in (("A", True), ("B", True), ("C", False)):
        (source / f"{name}.json").write_text(json.dumps({"is_invoice": is_invoice}))
    degrade.main()
    assert sorted(p.name for p in target.iterdir()) == ["A.jpg", "A.json"]
    assert (target / "A.jpg").read_bytes().startswith(b"\xff\xd8")  # it is a JPEG
    assert json.loads((target / "A.json").read_text()) == {"is_invoice": True}


def test_an_image_is_made_smaller(tmp_path, monkeypatch):
    source, target = folders(tmp_path, monkeypatch)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 400), False)
    pix.clear_with(255)
    (source / "P.jpg").write_bytes(pix.tobytes("jpeg"))
    (source / "P.json").write_text(json.dumps({"is_invoice": True}))
    degrade.main()
    assert pymupdf.Pixmap((target / "P.jpg").read_bytes()).width == 400 // 2 ** degrade.SHRINK
