"""Tests for validation, PDF rendering and the retry loop. No API calls: the model is replaced by a fake."""
import json
from pathlib import Path
from types import SimpleNamespace

import groq
import httpx
import pymupdf
import pytest

import extract

# Made-up invoice (the example from the brief), used as the "correct" answer.
GOOD = {
    "is_invoice": True, "vendor": "Acme Supplies Pvt Ltd", "invoice_number": "INV-2041",
    "invoice_date": "2026-03-14", "currency": "INR",
    "line_items": [{"description": "A4 paper, 500 sheets", "quantity": 10, "amount": 3200.00}],
    "tax_total": 576.00, "total": 3776.00,
}
BAD_TOTAL = {**GOOD, "total": 4000.00}  # does not add up


def js(payload):
    return json.dumps(payload)


def api_400(message):
    """A 400 the way the Groq SDK raises it."""
    response = httpx.Response(400, request=httpx.Request("POST", "https://api.groq.com/x"))
    return groq.BadRequestError(message, response=response, body=None)


class FakeClient:
    """Stands in for groq.Groq. Each queued item is the reply text, or an exception to raise."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)  # so client.chat.completions.create(...) works

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # copy: the real list grows
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                               usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200))


def make_pdf(path, pages):
    with pymupdf.open() as pdf:
        for _ in range(pages):
            pdf.new_page().insert_text((72, 72), "some text")  # a page with nothing on it counts as unreadable
        pdf.save(path)
    return path


def make_scan_pdf(path):
    """A PDF whose only content is an image and no text, like a scanned invoice (white with a dark block)."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200), False)
    pix.clear_with(255)
    pix.set_rect(pymupdf.IRect(20, 20, 120, 60), (0, 0, 0))
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_image(page.rect, stream=pix.tobytes("png"))
        pdf.save(path)
    return path


@pytest.fixture
def doc(tmp_path):
    return make_pdf(tmp_path / "x.pdf", 1)


# ---- validation ---------------------------------------------------------------------------

def test_good_invoice_has_no_problems():
    record, problems = extract.validate(GOOD)
    assert problems == []
    assert record["total"] == 3776.0


def test_totals_mismatch_is_reported():
    _, problems = extract.validate(BAD_TOTAL)
    assert len(problems) == 1 and "totals do not add up" in problems[0]


def test_totals_tolerance_edges():
    # One absolute tolerance of 0.05, whatever the size of the invoice.
    small = {**GOOD, "line_items": [{"description": "x", "quantity": 1, "amount": 10.0}], "tax_total": 0.0}
    assert extract.validate({**small, "total": 10.05})[1] == []   # exactly at the limit (float noise must not flag it)
    assert extract.validate({**small, "total": 10.06})[1] != []
    big = {**GOOD, "line_items": [{"description": "x", "quantity": 1, "amount": 40000.0}], "tax_total": 2000.0}
    assert extract.validate({**big, "total": 42000.05})[1] == []
    assert extract.validate({**big, "total": 42001.0})[1] != []   # a 1.00 error on a big invoice used to pass


def test_the_tolerance_is_not_a_percentage_of_the_total():
    # The regression behind the change: 1.00 off on an invoice in the tens of thousands must be reported.
    tax_slip = {**GOOD, "line_items": [{"description": "x", "quantity": 1, "amount": 40000.0}], "tax_total": 2001.0, "total": 42000.0}
    assert "totals do not add up" in extract.validate(tax_slip)[1][0]


def test_discount_row_and_null_quantity_are_fine():
    items = [{"description": "Work", "quantity": None, "amount": 100.0},
             {"description": "Discount", "quantity": None, "amount": -10.0}]
    assert extract.validate({**GOOD, "line_items": items, "tax_total": 9.0, "total": 99.0})[1] == []


def test_non_invoice_is_forced_to_null():
    record, problems = extract.validate({**GOOD, "is_invoice": False})  # model filled fields anyway
    assert problems == []
    assert record == {**dict.fromkeys(extract.INVOICE_FIELDS), "is_invoice": False}


@pytest.mark.parametrize("field,value", [
    ("invoice_date", "14/03/2026"), ("invoice_date", "2026-02-30"), ("invoice_date", "20260314"),
    ("currency", "inr"), ("currency", "RUPEES"),
])
def test_bad_format_is_reported(field, value):
    _, problems = extract.validate({**GOOD, field: value})
    assert problems and field in problems[0]


@pytest.mark.parametrize("field", extract.INVOICE_FIELDS)
def test_null_field_on_an_invoice_is_reported(field):
    _, problems = extract.validate({**GOOD, field: None})
    assert problems and field in problems[0]


def test_extra_or_missing_keys_are_reported():
    assert extract.validate({**GOOD, "surprise": 1})[1]
    missing = {k: v for k, v in GOOD.items() if k != "vendor"}
    assert extract.validate(missing)[1]


def test_invoice_has_the_eight_fields():
    assert set(extract.Invoice.model_fields) == {
        "is_invoice", "vendor", "invoice_number", "invoice_date", "currency", "line_items", "tax_total", "total"}


def test_reply_that_is_not_a_json_object_is_reported():
    assert extract.check_reply("sorry, I cannot read this")[0] is None
    assert extract.check_reply(None)[0] is None
    assert extract.check_reply("[1, 2]")[1]  # valid JSON, but not an invoice


SCHEMA = Path(__file__).parent.parent / "data" / "schema.json"


@pytest.mark.skipif(not SCHEMA.exists(), reason="sample pack not present (data/ is not committed)")
def test_core_fields_match_the_pack_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text())
    jsonschema.validate(extract.validate(GOOD)[0], schema)
    jsonschema.validate(extract.validate({**GOOD, "is_invoice": False})[0], schema)


# ---- tax lines (prompt v2) --------------------------------------------------------------------

V2_GOOD = {**{k: v for k, v in GOOD.items() if k != "tax_total"}, "tax_lines": [288.0, 288.0]}  # the same invoice, v2 shape


def test_tax_total_is_added_up_by_code_from_the_tax_lines():
    record, problems = extract.check_reply(js(V2_GOOD))
    assert problems == [] and record["tax_total"] == 576.0 and "tax_lines" not in record


def test_a_tax_total_the_model_wrote_anyway_is_overridden():
    record, _ = extract.check_reply(js({**V2_GOOD, "tax_total": 999.0}))
    assert record["tax_total"] == 576.0


def test_no_tax_is_an_empty_list_that_sums_to_zero():
    record, problems = extract.check_reply(js({**V2_GOOD, "tax_lines": [], "total": 3200.0}))
    assert problems == [] and record["tax_total"] == 0.0


def test_float_noise_in_the_sum_is_rounded_away():
    record, _ = extract.check_reply(js({**V2_GOOD, "tax_lines": [0.1, 0.2], "total": 3200.3}))
    assert record["tax_total"] == 0.3


def test_v2_non_invoice_with_null_tax_lines_is_fine():
    reply = {**dict.fromkeys(extract.INVOICE_FIELDS), "is_invoice": False, "tax_lines": None}
    del reply["tax_total"]
    record, problems = extract.check_reply(js(reply))
    assert problems == [] and record["is_invoice"] is False


def test_replies_without_tax_lines_pass_through_unchanged():  # prompt v1 keeps working
    record, problems = extract.check_reply(js(GOOD))
    assert problems == [] and record["tax_total"] == 576.0


@pytest.mark.parametrize("bad", ["576", [{"label": "GST", "amount": 576}], ["288", "288"], [True]])
def test_malformed_tax_lines_are_reported_and_retried(doc, bad):
    client = FakeClient(js({**V2_GOOD, "tax_lines": bad}), js(V2_GOOD))
    result = extract.extract_file(doc, client)
    assert len(client.calls) == 2 and result["needs_review"] is False
    assert "tax_lines must be a list of plain numbers" in client.calls[1]["messages"][-1]["content"]


def test_a_wrong_sum_in_the_model_reply_cannot_happen_any_more_but_a_wrong_line_is_still_caught(doc):
    # The code adds correctly, so a wrong tax_total can only come from a wrong printed amount, which the totals check sees.
    client = FakeClient(js({**V2_GOOD, "tax_lines": [288.0, 289.0]}), js(V2_GOOD))
    result = extract.extract_file(doc, client)
    assert len(client.calls) == 2 and result["tax_total"] == 576.0


# ---- the model's own doubt (prompt v3, experiment only) ----------------------------------------

def test_unsure_fields_flag_the_document_without_causing_a_retry(doc):
    client = FakeClient(js({**V2_GOOD, "low_confidence_fields": ["vendor", "total"]}))
    result = extract.extract_file(doc, client)
    assert len(client.calls) == 1  # doubt is a reason to look, not a failed check
    assert result["needs_review"] is True and result["review_reasons"] == ["model unsure about: vendor, total"]
    assert "low_confidence_fields" not in result and result["vendor"] == GOOD["vendor"]


def test_an_empty_doubt_list_is_no_flag(doc):
    result = extract.extract_file(doc, FakeClient(js({**V2_GOOD, "low_confidence_fields": []})))
    assert result["needs_review"] is False and result["review_reasons"] == []


@pytest.mark.parametrize("bad", [["shoe size"], "total", [1, {"a": 2}], None])
def test_unknown_or_malformed_doubt_is_ignored(doc, bad):
    result = extract.extract_file(doc, FakeClient(js({**V2_GOOD, "low_confidence_fields": bad})))
    assert result["needs_review"] is False


def test_a_failed_check_and_doubt_are_both_reported(doc):
    wrong = {**V2_GOOD, "tax_lines": [1.0], "low_confidence_fields": ["tax_total"]}
    client = FakeClient(js(wrong), js(wrong))
    result = extract.extract_file(doc, client)
    assert len(client.calls) == 2  # the failed check retried, the doubt did not add a retry of its own
    assert result["review_reasons"][0].startswith("totals do not add up")
    assert result["review_reasons"][-1] == "model unsure about: tax_total"


def test_attempts_1_turns_the_retry_off(doc, monkeypatch):
    monkeypatch.setattr(extract, "ATTEMPTS", 1)
    client = FakeClient(js(BAD_TOTAL), js(GOOD))
    result = extract.extract_file(doc, client)
    assert len(client.calls) == 1 and result["needs_review"] is True and result["total"] == 4000.0


# ---- documents to images ----------------------------------------------------------------------

def test_pdf_pages_are_rendered_to_png(tmp_path):
    images = extract.page_images(make_pdf(tmp_path / "a.pdf", 2))
    assert [media for media, _ in images] == ["image/png", "image/png"]
    assert all(data.startswith(b"\x89PNG") for _, data in images)


def test_too_many_pages_gives_an_error_record_without_calling_the_model(tmp_path):
    client = FakeClient()
    result = extract.extract_file(make_pdf(tmp_path / "big.pdf", extract.MAX_PAGES + 1), client)
    assert result["status"] == "error" and result["error"] == "too_many_pages"
    assert client.calls == []


def test_request_shape(doc):
    client = FakeClient(js(GOOD))
    extract.extract_file(doc, client)
    call = client.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"][0]["role"] == "system"
    parts = call["messages"][1]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[-1]["type"] == "text"


# ---- retry loop ---------------------------------------------------------------------------

def test_good_first_try_makes_one_call(doc):
    client = FakeClient(js(GOOD))
    result = extract.extract_file(doc, client)
    assert len(client.calls) == 1
    assert result["needs_review"] is False and result["review_reasons"] == []
    assert result["vendor"] == "Acme Supplies Pvt Ltd"


def test_retry_feeds_the_error_back_and_can_fix_it(doc):
    client = FakeClient(js(BAD_TOTAL), js(GOOD))
    result = extract.extract_file(doc, client)
    assert len(client.calls) == 2
    second = client.calls[1]["messages"]
    assert second[-2] == {"role": "assistant", "content": js(BAD_TOTAL)}
    assert "totals do not add up" in second[-1]["content"]
    assert result["total"] == 3776.0 and result["needs_review"] is False


def test_still_bad_after_retry_is_flagged_not_passed_silently(doc):
    client = FakeClient(js(BAD_TOTAL), js(BAD_TOTAL))
    result = extract.extract_file(doc, client)
    assert len(client.calls) == 2  # retries once, no more
    assert result["needs_review"] is True
    assert "totals do not add up" in result["review_reasons"][0]
    assert result["total"] == 4000.0  # the best attempt is still returned


def test_best_attempt_is_the_one_with_fewer_problems(doc):
    worse_second_try = {**BAD_TOTAL, "vendor": None, "currency": None}  # 2 problems vs 1 in the first try
    result = extract.extract_file(doc, FakeClient(js(BAD_TOTAL), js(worse_second_try)))
    assert result["vendor"] == GOOD["vendor"] and result["total"] == 4000.0  # kept the first attempt


def test_reply_that_is_not_json_is_retried(doc):
    client = FakeClient("not json at all", js(GOOD))
    result = extract.extract_file(doc, client)
    assert len(client.calls) == 2 and result["needs_review"] is False
    assert "not a valid JSON object" in client.calls[1]["messages"][-1]["content"]


def test_api_reporting_invalid_json_is_retried(doc):
    client = FakeClient(api_400("Error code: 400 - json_validate_failed"), js(GOOD))
    result = extract.extract_file(doc, client)
    assert len(client.calls) == 2 and result["needs_review"] is False
    assert client.calls[1]["messages"][-2] == {"role": "assistant", "content": "(no valid JSON)"}


def test_other_400_errors_are_not_hidden(doc):
    with pytest.raises(groq.BadRequestError):
        extract.extract_file(doc, FakeClient(api_400("Error code: 400 - image too large")))


def test_never_valid_json_gives_an_error_record(doc):
    result = extract.extract_file(doc, FakeClient("nope", "still nope"))
    assert result["status"] == "error" and result["error"] == "no_valid_json" and result["needs_review"] is True


def test_a_usable_first_attempt_survives_a_broken_retry(doc):
    result = extract.extract_file(doc, FakeClient(js(BAD_TOTAL), "garbage"))
    assert result["total"] == 4000.0 and result["needs_review"] is True


# ---- unreadable files -----------------------------------------------------------------------

UNREADABLE = {"status": "error", "error": "unreadable_file", "needs_review": True}
JUNK = bytes(range(256)) * 8  # 2 KB that is neither a PDF nor an image


@pytest.mark.parametrize("name,content", [
    ("empty.pdf", b""), ("empty.png", b""), ("empty.jpg", b""),
    ("garbage.pdf", JUNK), ("garbage.png", JUNK), ("garbage.jpg", JUNK),
    ("notes.txt", b"hello"), ("no_extension", b"hello"),
])
def test_unreadable_files_give_the_exact_error_record_and_no_api_call(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    client = FakeClient()
    assert extract.extract_file(path, client) == UNREADABLE
    assert client.calls == []


def test_blank_pdf_is_unreadable(tmp_path):
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(tmp_path / "blank.pdf")
    assert extract.extract_file(tmp_path / "blank.pdf", FakeClient()) == UNREADABLE


def test_scan_pdf_with_only_an_image_is_not_mistaken_for_blank(tmp_path):
    images = extract.page_images(make_scan_pdf(tmp_path / "scan.pdf"))
    assert len(images) == 1


@pytest.mark.parametrize("keep", [0.3, 0.5, 0.8])
def test_truncated_scan_is_unreadable(tmp_path, keep):
    path = make_scan_pdf(tmp_path / "scan.pdf")
    path.write_bytes(path.read_bytes()[: int(path.stat().st_size * keep)])
    client = FakeClient()
    assert extract.extract_file(path, client) == UNREADABLE
    assert client.calls == []


def test_api_rejecting_an_image_as_invalid_is_unreadable(doc):
    client = FakeClient(api_400("Error code: 400 - {'error': {'message': 'invalid image data'}}"))
    assert extract.extract_file(doc, client) == UNREADABLE
    assert len(client.calls) == 1


# ---- api failures ---------------------------------------------------------------------------

REQUEST = httpx.Request("POST", "https://api.groq.com/x")


def status_error(cls, code):
    return cls("error", response=httpx.Response(code, request=REQUEST), body=None)


@pytest.mark.parametrize("error,code", [
    (status_error(groq.RateLimitError, 429), "rate_limited"),
    (groq.APITimeoutError(request=REQUEST), "api_timeout"),
    (groq.APIConnectionError(request=REQUEST), "api_unavailable"),
    (status_error(groq.InternalServerError, 500), "api_unavailable"),
])
def test_api_failure_after_retries_gives_an_error_record(doc, error, code):
    assert extract.extract_file(doc, FakeClient(error)) == {"status": "error", "error": code, "needs_review": True}


def test_failed_retry_keeps_the_first_attempt_and_says_why(doc):
    result = extract.extract_file(doc, FakeClient(js(BAD_TOTAL), status_error(groq.RateLimitError, 429)))
    assert result["total"] == 4000.0 and result["needs_review"] is True
    assert result["review_reasons"][-1] == "the retry could not run: rate_limited"


def test_a_bad_key_stops_the_run_with_a_clear_message_instead_of_becoming_a_record(doc):
    with pytest.raises(SystemExit, match="GROQ_API_KEY"):
        extract.extract_file(doc, FakeClient(status_error(groq.AuthenticationError, 401)))


def test_a_rate_limit_is_waited_out_and_then_succeeds(doc, monkeypatch):
    monkeypatch.setattr(extract, "RATE_LIMIT_WAITS", (0, 0, 0))
    limited = status_error(groq.RateLimitError, 429)
    client = FakeClient(limited, limited, js(GOOD))
    result = extract.extract_file(doc, client)
    assert result["needs_review"] is False and len(client.calls) == 3
    assert len(extract.COST_LOG.read_text().splitlines()) == 1  # only the call that returned usage is logged


def test_a_rate_limit_that_never_clears_ends_as_an_error_record_after_every_wait(doc, monkeypatch):
    monkeypatch.setattr(extract, "RATE_LIMIT_WAITS", (0, 0, 0))
    limited = status_error(groq.RateLimitError, 429)
    client = FakeClient(limited, limited, limited, limited)
    assert extract.extract_file(doc, client)["error"] == "rate_limited"
    assert len(client.calls) == 4  # the first try plus one per wait


def test_waits_grow_exponentially_and_are_slept(doc, monkeypatch):
    slept = []
    monkeypatch.setattr(extract.time, "sleep", slept.append)
    monkeypatch.setattr(extract, "RATE_LIMIT_WAITS", (15, 30, 60))
    limited = status_error(groq.RateLimitError, 429)
    extract.extract_file(doc, FakeClient(limited, limited, limited, js(GOOD)))
    assert [s for s in slept if s] == [15, 30, 60]  # pacing adds a sleep(0.0) before the call, which is not a wait


def test_a_daily_limit_stops_the_run_at_once_instead_of_waiting(doc, monkeypatch):
    slept = []
    monkeypatch.setattr(extract.time, "sleep", slept.append)
    monkeypatch.setattr(extract, "RATE_LIMIT_WAITS", (15, 30, 60))
    message = "service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 197596. Please try again in 3m55.008s."
    daily = groq.RateLimitError(message, response=httpx.Response(429, request=REQUEST), body=None)
    client = FakeClient(daily)
    with pytest.raises(SystemExit, match="3m55"):
        extract.extract_file(doc, client)
    assert len(client.calls) == 1 and [s for s in slept if s] == []  # no waiting, no error record, the run stops


def test_calls_are_started_at_least_the_set_gap_apart(doc, monkeypatch):
    sleeps, clock = [], iter([100.0, 100.0, 110.0, 110.0])  # two reads of monotonic() per call
    monkeypatch.setattr(extract.time, "sleep", sleeps.append)
    monkeypatch.setattr(extract.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(extract, "SECONDS_BETWEEN_CALLS", 30.0)
    extract.extract_file(doc, FakeClient(js(GOOD), js(GOOD)))
    extract.extract_file(doc, FakeClient(js(GOOD), js(GOOD)))
    assert sleeps == [0.0, 20.0]  # the first call goes straight away, the second waits until 30 s after the first


def test_the_default_prompt_is_v2():
    assert extract.PROMPT_FILE.name == "extract_v2.md" and extract.PROMPT_FILE.exists()


# ---- command line ---------------------------------------------------------------------------

def test_batch_writes_one_file_per_document_and_keeps_going(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "in"
    folder.mkdir()
    make_pdf(folder / "good.pdf", 1)
    (folder / "empty.pdf").write_bytes(b"")
    (folder / "truth.json").write_text("{}")  # ground-truth files sit next to the documents: skipped
    (folder / "notes.txt").write_text("x")
    monkeypatch.setattr(extract, "make_client", lambda: FakeClient(js(GOOD)))
    out = tmp_path / "out"
    assert extract.main(["--batch", str(folder), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["empty.json", "good.json"]
    assert json.loads((out / "good.json").read_text())["vendor"] == GOOD["vendor"]
    assert json.loads((out / "empty.json").read_text()) == UNREADABLE
    err = capsys.readouterr().err
    assert "'ok': 1" in err and "'unreadable_file': 1" in err


def test_batch_resume_keeps_real_answers_and_redoes_only_what_got_none(tmp_path, monkeypatch, capsys):
    folder, out = tmp_path / "in", tmp_path / "out"
    folder.mkdir()
    out.mkdir()
    for name in ("done", "limited", "new"):
        make_pdf(folder / f"{name}.pdf", 1)
    (folder / "bad.pdf").write_bytes(b"")
    (out / "done.json").write_text(json.dumps({"vendor": "Kept", "needs_review": False}))          # a real answer
    (out / "bad.json").write_text(json.dumps(UNREADABLE))                                          # also a real answer
    (out / "limited.json").write_text(json.dumps(extract.error_record("rate_limited")))             # never answered
    client = FakeClient(js(GOOD), js(GOOD))  # exactly two documents should be asked: limited and new
    monkeypatch.setattr(extract, "make_client", lambda: client)
    assert extract.main(["--batch", str(folder), "--out", str(out), "--resume"]) == 0
    assert len(client.calls) == 2
    assert json.loads((out / "done.json").read_text())["vendor"] == "Kept"
    assert json.loads((out / "bad.json").read_text()) == UNREADABLE
    assert json.loads((out / "limited.json").read_text())["vendor"] == GOOD["vendor"]
    assert json.loads((out / "new.json").read_text())["vendor"] == GOOD["vendor"]
    assert "'kept': 2" in capsys.readouterr().err


def test_batch_without_resume_runs_everything_again(tmp_path, monkeypatch):
    folder, out = tmp_path / "in", tmp_path / "out"
    folder.mkdir()
    out.mkdir()
    make_pdf(folder / "done.pdf", 1)
    (out / "done.json").write_text(json.dumps({"vendor": "Old", "needs_review": False}))
    monkeypatch.setattr(extract, "make_client", lambda: FakeClient(js(GOOD)))
    extract.main(["--batch", str(folder), "--out", str(out)])
    assert json.loads((out / "done.json").read_text())["vendor"] == GOOD["vendor"]


def test_single_file_prints_only_the_record_and_exits_zero(doc, monkeypatch, capsys):
    monkeypatch.setattr(extract, "make_client", lambda: FakeClient(js(GOOD)))
    assert extract.main([str(doc)]) == 0
    assert json.loads(capsys.readouterr().out)["invoice_number"] == "INV-2041"


def test_single_file_exits_non_zero_for_an_error_record(tmp_path, capsys):
    bad = tmp_path / "empty.pdf"
    bad.write_bytes(b"")
    assert extract.main([str(bad)]) == 1
    assert json.loads(capsys.readouterr().out) == UNREADABLE


def test_missing_file_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit):
        extract.main([str(tmp_path / "nope.pdf")])


# ---- cost log -----------------------------------------------------------------------------

def test_every_call_including_the_retry_is_logged(doc):
    extract.extract_file(doc, FakeClient(js(BAD_TOTAL), js(GOOD)))
    rows = [json.loads(line) for line in extract.COST_LOG.read_text().splitlines()]
    assert [r["attempt"] for r in rows] == [1, 2]
    assert rows[0]["cost_usd"] == pytest.approx((1000 * 0.80 + 200 * 4.00) / 1_000_000)  # qwen3.8-27b list price


def test_spend_cap_stops_before_calling_the_model(doc):
    extract.COST_LOG.write_text(json.dumps({"cost_usd": 2.0}) + "\n")
    client = FakeClient(js(GOOD))
    with pytest.raises(SystemExit):
        extract.extract_file(doc, client)
    assert client.calls == []
