"""Tests for the eval harness: the matching rules, the scoring, and the saved results. Made-up data only."""
import json

import pytest

import eval as ev
import extract
from test_extract import FakeClient, js, make_pdf

ITEMS = [
    {"description": "A4 paper, 500 sheets", "quantity": 10, "amount": 3200.0},
    {"description": "Stapler, heavy duty", "quantity": 2, "amount": 400.0},
    {"description": "Courier charge", "quantity": None, "amount": 50.0},
]
TRUTH = {
    "is_invoice": True, "vendor": "Acme Supplies Pvt Ltd", "invoice_number": "INV-2041", "invoice_date": "2026-03-14",
    "currency": "INR", "line_items": ITEMS, "tax_total": 657.0, "total": 4307.0,
}
NON_INVOICE = {**dict.fromkeys(ev.FIELDS), "is_invoice": False}


@pytest.fixture(autouse=True)
def private_results(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "RESULTS", tmp_path / "results")


# ---- field matching rules -----------------------------------------------------------------------

@pytest.mark.parametrize("got,expected", [
    ("ACME SUPPLIES PVT. LTD.", "Acme Supplies Pvt Ltd"),   # case and punctuation
    ("Acme Supplies", "Acme Supplies Pvt Ltd"),             # suffix dropped
    ("Brown and Lee Associates", "Brown & Lee Associates"),  # & versus and
    ("Larkspur Systems GmbH", "Larkspur Systems"),
    ("Cedar Print and Pak", "Cedar Print & Pack"),           # one-letter typo, ratio above 90
    (None, None),
])
def test_vendor_matches(got, expected):
    assert ev.same_vendor(got, expected)


@pytest.mark.parametrize("got,expected", [
    ("Acme Supplies Pvt Ltd", "Acme Stationers Pvt Ltd"),
    (None, "Acme"), ("Acme", None),
])
def test_vendor_does_not_match(got, expected):
    assert not ev.same_vendor(got, expected)


def test_invoice_number_is_exact_after_trim_and_uppercase():
    assert ev.same_number("  inv-2041 ", "INV-2041")
    assert not ev.same_number("INV 2041", "INV-2041")
    assert ev.same_number(None, None) and not ev.same_number(None, "INV-1")


def test_amounts_are_compared_within_a_cent():
    assert ev.close(100.00, 100.01) and ev.close(100.01, 100.00)
    assert not ev.close(100.00, 100.02)
    assert ev.close(None, None) and not ev.close(None, 0.0) and not ev.close("abc", 1.0)


# ---- line items ---------------------------------------------------------------------------------

def test_items_match_in_any_order():
    matched, missing, extra = ev.match_items(ITEMS, list(reversed(ITEMS)))
    assert (matched, missing, extra) == (3, [], [])


def test_description_only_needs_to_be_similar():
    pred = [{**ITEMS[0], "description": "A4 paper 500 sheets"}, ITEMS[1], ITEMS[2]]
    assert ev.match_items(ITEMS, pred)[0] == 3


def test_a_wrong_amount_is_one_missing_and_one_extra():
    pred = [{**ITEMS[0], "amount": 3300.0}, ITEMS[1], ITEMS[2]]
    matched, missing, extra = ev.match_items(ITEMS, pred)
    assert matched == 2 and missing == [ITEMS[0]] and len(extra) == 1


def test_same_numbers_with_a_different_description_do_not_match():
    pred = [{**ITEMS[0], "description": "Courier charge"}]
    assert ev.match_items([ITEMS[0]], pred)[0] == 0


def test_quantity_null_only_matches_null():
    assert ev.match_items([ITEMS[2]], [{**ITEMS[2], "quantity": 1}])[0] == 0
    assert ev.match_items([ITEMS[2]], [ITEMS[2]])[0] == 1


def test_duplicate_rows_are_matched_one_to_one():
    matched, missing, extra = ev.match_items([ITEMS[0], ITEMS[0]], [ITEMS[0]])
    assert matched == 1 and len(missing) == 1 and extra == []


# ---- scoring one document -----------------------------------------------------------------------

def test_perfect_prediction():
    score = ev.score_document(TRUTH, TRUTH)
    assert score["exact"] and all(score["fields"].values()) and score["diff"] == []


def test_wrong_total_fails_only_that_field():
    score = ev.score_document({**TRUTH, "total": 4000.0}, TRUTH)
    assert [f for f, ok in score["fields"].items() if not ok] == ["total"]
    assert not score["exact"] and "total: expected 4307.0, got 4000.0" in score["diff"][0]


def test_error_record_gets_no_credit_even_for_null_fields():
    score = ev.score_document(extract.error_record("unreadable_file"), NON_INVOICE)
    assert not any(score["fields"].values()) and not score["exact"]
    assert "unreadable_file" in score["diff"][0]


def test_non_invoice_scores():
    assert ev.score_document(NON_INVOICE, NON_INVOICE)["exact"]
    wrong = ev.score_document(TRUTH, NON_INVOICE)  # called an invoice what is not one
    assert not wrong["fields"]["is_invoice"] and not wrong["fields"]["vendor"]


def test_malformed_line_items_do_not_crash():
    score = ev.score_document({**TRUTH, "line_items": "three items"}, TRUTH)
    assert score["fields"]["line_items"] is False and score["items"]["predicted"] == 0


def test_item_with_a_wrong_row_shows_missing_and_extra_in_the_diff():
    pred = {**TRUTH, "line_items": [{**ITEMS[0], "amount": 3300.0}, ITEMS[1], ITEMS[2]]}
    diff = "\n".join(ev.score_document(pred, TRUTH)["diff"])
    assert "matched 2 of 3 expected" in diff and "missing:" in diff and "extra:" in diff


# ---- aggregate and saved results ----------------------------------------------------------------

def two_documents():
    truth = {"A": TRUTH, "B": TRUTH}
    predictions = {"A": TRUTH, "B": {**TRUTH, "total": 4000.0, "needs_review": True, "line_items": ITEMS[:2]}}
    return {d: ev.score_document(predictions[d], truth[d]) for d in truth}, predictions


def test_counts_and_item_precision_recall():
    scores, predictions = two_documents()
    public, _ = ev.build_result(scores, predictions, "test", None)
    assert public["accuracy"]["total"] == {"correct": 1, "total": 2, "accuracy": 0.5}
    assert public["accuracy"]["document_exact_match"]["correct"] == 1
    assert public["accuracy"]["line_items"]["correct"] == 1
    # 3 + 2 matched of 5 predicted and 6 expected rows
    assert public["line_items_item_level"] == {"matched": 5, "predicted": 5, "expected": 6, "precision": 1.0, "recall": 0.8333}
    assert public["per_document"]["B"]["needs_review"] is True


def test_the_committable_result_holds_no_document_values():
    scores, predictions = two_documents()
    public, details = ev.build_result(scores, predictions, "test", None)
    text = json.dumps(public)
    for value in ("Acme", "INV-2041", "Stapler", "4307", "4000"):
        assert value not in text
    assert "4000.0" in json.dumps(details)  # the private file does have them


# ---- review flags -------------------------------------------------------------------------------

def test_review_detail_splits_failed_checks_from_the_models_doubt():
    reasons = ["totals do not add up: ...", "invoice_date: must be written YYYY-MM-DD", "model unsure about: vendor, total"]
    assert ev.review_detail({"review_reasons": reasons}) == {"failed_checks": 2, "unsure": ["vendor", "total"]}
    assert ev.review_detail({}) == {"failed_checks": 0, "unsure": []}  # an error record has no reasons


def flagged_run():
    """Four documents: A right and unflagged, B wrong and failed a check, C wrong and doubted, D right but doubted."""
    wrong_total = {**TRUTH, "total": 4000.0}
    truth = {name: TRUTH for name in "ABCD"}
    predictions = {
        "A": TRUTH,
        "B": {**wrong_total, "needs_review": True, "review_reasons": ["totals do not add up"]},
        "C": {**wrong_total, "needs_review": True, "review_reasons": ["model unsure about: total"]},
        "D": {**TRUTH, "needs_review": True, "review_reasons": ["model unsure about: vendor"]},
    }
    scores = {d: ev.score_document(predictions[d], truth[d]) for d in truth}
    return ev.build_result(scores, predictions, "test", None)[0]


def test_flag_summary_counts_catches_false_alarms_and_fields():
    text = "\n".join(ev.flag_summary(flagged_run()))
    assert "2 of 4 documents have at least one wrong field" in text
    assert "flagged by a failed check: 1 documents, 1 of the 2 wrong ones, 0 false alarms" in text
    assert "flagged by the model's own doubt: 2 documents, 1 of the 2 wrong ones, 1 false alarms" in text
    assert "flagged by either: 3 documents, 2 of the 2 wrong ones, 1 false alarms" in text
    # it doubted total (C, really wrong) and vendor (D, fine): 2 doubted, 1 wrong; of the 2 wrong fields (B and C totals) it named 1
    assert "the model doubted 2 fields and 1 of them really were wrong; it doubted 1 of the 2 wrong fields" in text


def test_the_flag_summary_is_left_out_of_runs_saved_before_it_existed():
    public = flagged_run()
    for d in public["per_document"].values():
        d.pop("failed_checks"), d.pop("unsure")
    assert ev.flag_summary(public) == []
    assert "review flags" not in ev.format_table(public)


def test_the_table_shows_the_flag_summary():
    assert "review flags: 2 of 4" in ev.format_table(flagged_run())


# ---- command line -------------------------------------------------------------------------------

@pytest.fixture
def folders(tmp_path):
    labelled, predictions = tmp_path / "labelled", tmp_path / "predictions"
    labelled.mkdir()
    predictions.mkdir()
    make_pdf(labelled / "A.pdf", 1)
    for name, truth in (("A", TRUTH), ("B", NON_INVOICE)):
        (labelled / f"{name}.json").write_text(json.dumps(truth))
        (predictions / f"{name}.json").write_text(json.dumps(truth))
    return labelled, predictions


def run(folders, *extra):
    labelled, predictions = folders
    return ev.main(["--labelled", str(labelled), "--predictions", str(predictions), *extra])


def test_scoring_existing_predictions_saves_three_files(folders, capsys):
    assert run(folders, "--name", "t1") == 0
    assert sorted(p.name for p in ev.RESULTS.iterdir()) == ["details", "t1.json", "t1.txt"]
    assert (ev.RESULTS / "details" / "t1.json").exists()
    assert "100.0%" in capsys.readouterr().out
    assert json.loads((ev.RESULTS / "t1.json").read_text())["accuracy"]["document_exact_match"]["correct"] == 2


def test_scored_files_do_not_claim_a_prompt(folders):
    run(folders, "--name", "p")
    assert json.loads((ev.RESULTS / "p.json").read_text())["prompt"].startswith("not recorded")


def test_failures_are_printed_unless_no_diffs(folders, capsys):
    (folders[1] / "A.json").write_text(json.dumps({**TRUTH, "total": 4000.0}))
    run(folders, "--name", "with")
    assert "total: expected 4307.0, got 4000.0" in capsys.readouterr().out
    run(folders, "--name", "without", "--no-diffs")
    out = capsys.readouterr().out
    assert "total: expected" not in out and "whole document exact" in out
    # the details are still saved, and --show prints them later
    ev.main(["--show", "without"])
    assert "total: expected 4307.0, got 4000.0" in capsys.readouterr().out


def test_a_saved_name_is_never_overwritten(folders):
    run(folders, "--name", "baseline")
    with pytest.raises(SystemExit):
        run(folders, "--name", "baseline")


def test_missing_prediction_counts_as_an_error(folders, capsys):
    (folders[1] / "B.json").unlink()
    run(folders, "--name", "m")
    public = json.loads((ev.RESULTS / "m.json").read_text())
    assert public["per_document"]["B"]["error"] == "missing_prediction"
    assert not any(public["per_document"]["B"]["fields"].values())


def test_resume_asks_again_only_for_documents_that_got_no_answer(folders, monkeypatch, capsys):
    labelled, predictions = folders
    make_pdf(labelled / "B.pdf", 1)
    (predictions / "A.json").write_text(json.dumps({**TRUTH, "total": 4000.0}))  # a real, wrong answer
    (predictions / "B.json").write_text(json.dumps(extract.error_record("rate_limited")))  # never got an answer
    run(folders, "--name", "first")
    client = FakeClient(js(NON_INVOICE))
    monkeypatch.setattr(extract, "make_client", lambda: client)
    assert ev.main(["--labelled", str(labelled), "--resume", "first", "--name", "second"]) == 0
    assert len(client.calls) == 1  # only B was asked again
    second = json.loads((ev.RESULTS / "second.json").read_text())
    assert second["per_document"]["A"]["exact"] is False  # the wrong answer was kept, not re-rolled
    assert second["per_document"]["B"]["exact"] is True
    assert "resumed from first" in second["mode"] and second["usage"]["calls"] == 1


def test_resume_with_nothing_to_ask_again_makes_no_model_calls(folders, monkeypatch):
    run(folders, "--name", "first")
    monkeypatch.setattr(extract, "make_client", lambda: pytest.fail("the model must not be called"))
    assert ev.main(["--labelled", str(folders[0]), "--resume", "first", "--name", "second"]) == 0


def test_running_the_model_scores_it_and_records_usage(folders, monkeypatch, capsys):
    labelled, _ = folders
    truth = {"is_invoice": True, "vendor": "Acme Supplies Pvt Ltd", "invoice_number": "INV-2041", "invoice_date": "2026-03-14",
             "currency": "INR", "line_items": [{"description": "A4 paper, 500 sheets", "quantity": 10, "amount": 3200.0}],
             "tax_total": 576.0, "total": 3776.0}
    (labelled / "A.json").write_text(json.dumps(truth))
    (labelled / "B.json").unlink()  # one document, so one reply is enough
    monkeypatch.setattr(extract, "make_client", lambda: FakeClient(js(truth)))
    assert ev.main(["--labelled", str(labelled), "--name", "live"]) == 0
    public = json.loads((ev.RESULTS / "live.json").read_text())
    assert public["mode"] == "ran the model" and public["usage"]["calls"] == 1
    assert public["accuracy"]["document_exact_match"]["correct"] == 1
