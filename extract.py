"""Invoice extractor: a PDF or image goes in, validated JSON comes out.

    python extract.py <file> > out.json
    python extract.py --batch data/labelled data/unlabelled     (writes outputs/<name>.json)
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import groq
import pymupdf  # PyMuPDF: renders PDF pages to images
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")  # so GROQ_API_KEY and MODEL can live in .env

# The only Groq model that accepts images (checked against the live model list, 2026-11-20).
MODEL = os.environ.get("MODEL", "qwen/qwen3.8-27b")
# The prompt lives in a file so every experiment is one diffable file, logged in experiments.md.
PROMPT_FILE = HERE / os.environ.get("PROMPT_FILE", "prompts/extract_v2.md")  # v1 is kept to reproduce the baseline
COST_LOG = HERE / "results" / "cost_log.jsonl"
SPEND_CAP_USD = float(os.environ.get("SPEND_CAP_USD", "1.5"))
# 2 = one try plus one retry. Only the review-flag experiment sets 1, to judge first attempts before the retry repairs them.
ATTEMPTS = int(os.environ.get("ATTEMPTS", "2"))
# Tags every API call made by this process, so cost can later be averaged per run.
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")

# The totals check allows this much difference between "line amounts + tax" and the printed total.
# Why so small: every printed amount is already rounded to the cent and the printed total is the sum of
# printed amounts, so a correct extraction balances to the cent (the ground truth of all 10 labelled
# documents balances exactly). 0.05 leaves room for a few rounded lines. It is deliberately NOT a
# percentage of the total: the first version allowed 0.1% of the total, which tolerated errors of
# several tens on an invoice in the tens of thousands and let a wrong tax_total pass unflagged
# (see experiments.md).
TOTALS_TOLERANCE = 0.05

# USD per million tokens (input, output).
# Source: Groq's model page https://console.groq.com/docs/model/qwen/qwen3.8-27b and the "pricing"
# field of GET https://api.groq.com/openai/v1/models, both read 2026-11-20.
# The free tier charges nothing. This is the list price, so the cost log and report show what a
# paid run would cost, and the spend cap counts these list-price dollars.
PRICES = {"qwen/qwen3.8-27b": (0.80, 4.00)}

# Seconds to wait after a 429, one entry per extra try (exponential: 15, 30, 60). The free tier counts
# tokens per minute, and its output-token 429 comes without a Retry-After header, so the SDK's own
# backoff (0.5 to 2 s) is over long before the window clears. Found when 3 of 10 baseline documents
# came back rate_limited.
RATE_LIMIT_WAITS = (15, 30, 60)

# The free tier allows about 8,000 tokens per minute (and 1,000 output tokens per minute) and one document
# uses about 3,000, so more than 2 documents a minute run into the limit. Starting calls at least this many
# seconds apart avoids most 429s instead of waiting them out; two documents still outlasted every wait
# without it. Set SECONDS_BETWEEN_CALLS=0 on a paid tier.
SECONDS_BETWEEN_CALLS = float(os.environ.get("SECONDS_BETWEEN_CALLS", "30"))
_last_call = 0.0  # when the previous call started, so pacing carries across documents in one run

MAX_PAGES = 3    # the model accepts at most 3 images per request
RENDER_DPI = 150  # sharp enough for small print, small enough to keep image tokens low
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
SUPPORTED = {".pdf", *MEDIA_TYPES}

# Error records that mean "no answer was ever obtained" (a wrong answer is not one): safe to ask again.
NO_ANSWER_ERRORS = {"rate_limited", "api_timeout", "api_unavailable"}
UNSURE_PREFIX = "model unsure about: "  # how the model's own doubts appear in review_reasons (prompt v3 only)


# ---- schema -------------------------------------------------------------------------------

class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")  # schema.json says additionalProperties: false
    description: str
    quantity: float | None  # null when the printed row has no quantity
    amount: float


class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_invoice: bool
    vendor: str | None
    invoice_number: str | None
    invoice_date: str | None
    currency: str | None
    line_items: list[LineItem] | None
    tax_total: float | None
    total: float | None

    @model_validator(mode="before")
    @classmethod
    def non_invoice_is_empty(cls, data):
        # The prompt asks for this too, but a non-invoice must never carry invoice data,
        # whatever the model returned, so it is enforced here.
        if isinstance(data, dict) and data.get("is_invoice") is False:
            return {**dict.fromkeys(cls.model_fields), "is_invoice": False}
        return data

    @field_validator("invoice_date")
    @classmethod
    def iso_date(cls, v):
        if v is not None:
            # fromisoformat alone also accepts "20260314"; we want exactly YYYY-MM-DD
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise ValueError("must be written YYYY-MM-DD")
            date.fromisoformat(v)  # rejects impossible dates such as 2026-02-30
        return v

    @field_validator("currency")
    @classmethod
    def three_letter_code(cls, v):
        if v is not None and not re.fullmatch(r"[A-Z]{3}", v):
            raise ValueError("must be a 3-letter uppercase code such as INR")
        return v


# Everything except is_invoice has to be present on a real invoice.
INVOICE_FIELDS = [f for f in Invoice.model_fields if f != "is_invoice"]


# ---- checks -------------------------------------------------------------------------------

def find_problems(inv):
    """Business checks that the schema cannot express. Returns a list of readable problems."""
    if not inv.is_invoice:
        return []
    problems = [f"{name} is null, but an invoice needs it" for name in INVOICE_FIELDS if getattr(inv, name) is None]
    if problems:
        return problems  # the totals check needs every number
    items_sum = sum(item.amount for item in inv.line_items)
    # Round to the cent first: in floating point 10.0 + 0.0 - 10.05 is 0.05000000000000071, not 0.05.
    if abs(round(items_sum + inv.tax_total - inv.total, 2)) > TOTALS_TOLERANCE:
        problems.append(
            f"totals do not add up: line amounts sum to {items_sum:.2f} and tax_total is {inv.tax_total:.2f}, "
            f"which gives {items_sum + inv.tax_total:.2f}, but total is {inv.total:.2f} "
            f"(allowed difference {TOTALS_TOLERANCE:.2f})"
        )
    return problems


def validate(raw):
    """Model output (a dict) -> (record dict, problems). Never raises on bad model output."""
    try:
        inv = Invoice.model_validate(raw)
    except ValidationError as e:
        return raw, [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
    return inv.model_dump(), find_problems(inv)


def check_reply(reply):
    """The model's raw text -> (record or None, problems). JSON mode makes bad JSON rare, not impossible."""
    try:
        raw = json.loads(reply)
    except (TypeError, ValueError):
        return None, ["the reply was not a valid JSON object"]
    unsure = raw.pop("low_confidence_fields", None) if isinstance(raw, dict) else None  # prompt v3 only
    extra = []
    if isinstance(raw, dict) and "tax_lines" in raw:
        # Prompt v2 asks for each printed tax amount instead of their sum: models add badly, code does not.
        # Replies without tax_lines (prompt v1) pass through untouched.
        lines = raw.pop("tax_lines")
        if lines is None or (isinstance(lines, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in lines)):
            raw["tax_total"] = None if lines is None else round(sum(lines), 2)  # rounded: 0.1 + 0.2 is 0.30000000000000004
        else:
            raw["tax_total"] = None  # an invoice with no tax_total is reported below, and the model is told why
            extra = ["tax_lines must be a list of plain numbers, one per printed tax amount"]
    record, problems = validate(raw)
    if unsure is not None:
        # Kept apart from `problems`: doubt is a reason to look at the document, not a failed check to retry.
        names = [f for f in unsure if isinstance(f, str) and f in Invoice.model_fields] if isinstance(unsure, list) else []
        record = {**record, "low_confidence_fields": names}
    return record, extra + problems


def error_record(code, detail=None):
    """The one shape every failure uses, so batch runs and the eval can recognise it."""
    record = {"status": "error", "error": code, "needs_review": True}
    if detail:
        record["detail"] = detail
    return record


# ---- cost ---------------------------------------------------------------------------------

def cost_usd(usage):
    p_in, p_out = PRICES[MODEL]
    return (usage.prompt_tokens * p_in + usage.completion_tokens * p_out) / 1_000_000


def spent(run=None):
    """List-price dollars logged so far, for every run or just one."""
    if not COST_LOG.exists():
        return 0.0
    rows = [json.loads(line) for line in COST_LOG.read_text().splitlines()]
    return sum(r["cost_usd"] for r in rows if run is None or r.get("run") == run)


def log_cost(name, attempt, usage):
    # Logged from the usage the API reports, for every call, retries included.
    COST_LOG.parent.mkdir(exist_ok=True)
    row = {
        "run": RUN_ID, "file": name, "attempt": attempt, "model": MODEL, "prompt": PROMPT_FILE.name,
        "input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens, "cost_usd": cost_usd(usage),
    }
    with COST_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


# ---- documents to images --------------------------------------------------------------------

class UnreadableFile(Exception):
    """Empty, corrupt, blank, or not a PDF or image: nothing the model could read."""


class TooManyPages(Exception):
    pass


def pdf_pages(path):
    with pymupdf.open(path) as pdf:  # raises on empty or garbage files
        if len(pdf) == 0:  # a truncated PDF can open with no pages at all
            raise UnreadableFile("the PDF has no pages")
        if len(pdf) > MAX_PAGES:  # refuse rather than silently drop pages
            raise TooManyPages(f"{len(pdf)} pages, the model accepts at most {MAX_PAGES} images per request")
        pixmaps = [page.get_pixmap(dpi=RENDER_DPI) for page in pdf]
    # A truncated scan can open as a page that renders blank (it may even still list an image whose
    # data is gone). Sending that would turn damage into a confident "not an invoice", so a document
    # whose pages are all one flat colour counts as unreadable.
    if all(pix.color_count() == 1 for pix in pixmaps):
        raise UnreadableFile("every page renders blank")
    return [("image/png", pix.tobytes("png")) for pix in pixmaps]


def page_images(path):
    """[(media type, bytes)], one per page. The model reads images, not PDFs, so a PDF is rendered."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return pdf_pages(path)
        if suffix in MEDIA_TYPES:
            pymupdf.Pixmap(str(path))  # decodes the image, so a corrupt or empty one fails here, before any API call
            return [(MEDIA_TYPES[suffix], path.read_bytes())]
    except (RuntimeError, pymupdf.mupdf.FzErrorBase) as e:  # what PyMuPDF raises for files it cannot read
        raise UnreadableFile(str(e)[:100]) from e
    raise UnreadableFile(f"not a PDF or image ({suffix or 'no extension'})")


def image_part(media_type, data):
    url = f"data:{media_type};base64,{base64.standard_b64encode(data).decode()}"
    return {"type": "image_url", "image_url": {"url": url}}


# ---- model call ---------------------------------------------------------------------------

def make_client():
    if not os.environ.get("GROQ_API_KEY"):
        sys.exit("GROQ_API_KEY is not set. Copy .env.example to .env and add your key.")
    # The SDK waits out a 429 as long as its Retry-After header says (up to 60s), else backs off exponentially.
    return groq.Groq(max_retries=3)


def pace():
    global _last_call
    time.sleep(max(0.0, _last_call + SECONDS_BETWEEN_CALLS - time.monotonic()))
    _last_call = time.monotonic()


def create_with_waits(client, **kwargs):
    """The SDK already retries blips quickly. On top of that, wait out a rate limit window."""
    for wait in RATE_LIMIT_WAITS:
        try:
            return client.chat.completions.create(**kwargs)
        except groq.RateLimitError as e:
            if "tokens per day" in str(e):  # the daily cap: waiting seconds cannot help, and every later document would fail too
                again = re.search(r"try again in ([0-9hms.]+)", str(e))
                sys.exit("Daily token limit reached (the free tier allows 200,000 tokens a day, refilled continuously). "
                         f"Try again in about {again.group(1) if again else 'a while'}.")
            print(f"rate limited, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    return client.chat.completions.create(**kwargs)  # last try: a RateLimitError here is the caller's problem


def call_model(client, messages, name, attempt):
    """Returns the reply text, or None when the API itself reports that the model's output was not valid JSON."""
    if MODEL not in PRICES:
        sys.exit(f"No price known for {MODEL}. Add it to PRICES in extract.py so cost can be logged.")
    if spent() >= SPEND_CAP_USD:
        sys.exit(f"Spend cap of ${SPEND_CAP_USD} reached (${spent():.4f} logged). Stop and ask before spending more.")
    pace()
    try:
        response = create_with_waits(
            client,
            model=MODEL,
            messages=messages,
            max_tokens=8000,
            temperature=0,  # extraction should be repeatable, so runs can be compared
            response_format={"type": "json_object"},  # forced tool calls were unreliable on this model
        )
    except groq.AuthenticationError:  # a wrong or placeholder key fails every document the same way, so stop with a clear message
        sys.exit("Groq rejected the API key (HTTP 401). Put your key after GROQ_API_KEY= in .env (see .env.example).")
    except groq.BadRequestError as e:
        text = str(e)
        if "json_validate_failed" in text:
            return None  # the model's output was not JSON: a failed attempt, not a crash
        if "invalid image data" in text or "invalid base64" in text:  # what the live API says for a corrupt image
            raise UnreadableFile("the API rejected the image as invalid") from e
        raise  # any other 400 may be our own bad request, so do not hide it
    log_cost(name, attempt, response.usage)
    return response.choices[0].message.content


# What the SDK raises once its own retries are used up. A timeout is a kind of connection error.
# Anything else (bad key, wrong model name) is a setup problem for the whole run, so it is not caught.
API_FAILURES = (groq.RateLimitError, groq.APIConnectionError, groq.InternalServerError)


def failure_code(e):
    if isinstance(e, groq.RateLimitError):
        return "rate_limited"
    if isinstance(e, groq.APITimeoutError):
        return "api_timeout"
    return "api_unavailable"  # connection failures and 5xx


def run_extraction(path, client):
    images = page_images(path)  # before the client, so a bad file never needs an API key
    client = client or make_client()
    messages = [
        {"role": "system", "content": PROMPT_FILE.read_text(encoding="utf-8")},
        {"role": "user", "content": [image_part(*image) for image in images]
         + [{"type": "text", "text": "Extract the data from this document."}]},
    ]
    attempts = []  # (record or None, problems) for each try
    failure = None  # code of an API failure that cut the run short
    for attempt in range(1, ATTEMPTS + 1):  # one retry at most
        try:
            reply = call_model(client, messages, path.name, attempt)
        except API_FAILURES as e:
            failure = failure_code(e)
            print(f"{path.name}: {failure}, the SDK's retries ran out", file=sys.stderr)
            break
        record, problems = check_reply(reply)
        attempts.append((record, problems))
        if not problems or attempt == ATTEMPTS:
            break
        # Count only: the text of a failure can contain document values, and a run kept unread must not leak them.
        print(f"{path.name}: attempt 1 failed {len(problems)} check(s), retrying once", file=sys.stderr)
        # Give the model its own answer back plus the exact failures, so it can fix them.
        messages += [
            {"role": "assistant", "content": reply or "(no valid JSON)"},
            {"role": "user", "content": "Your reply failed these checks:\n"
             + "\n".join(f"- {p}" for p in problems)
             + "\nRe-read the document, correct any misread value, and reply with the corrected JSON object only. "
             "If the document itself really does not add up, keep the printed values."},
        ]
    usable = [a for a in attempts if a[0] is not None]
    if not usable:
        if failure:
            return error_record(failure)
        return error_record("no_valid_json", "the model never returned a valid JSON object")
    # Best attempt = fewest problems; reversed() makes the later attempt win a tie.
    record, problems = min(reversed(usable), key=lambda a: len(a[1]))
    reasons = list(problems)
    if failure:  # attempt 1 worked but needed a fix, and the retry could not run
        reasons.append(f"the retry could not run: {failure}")
    unsure = record.pop("low_confidence_fields", [])  # prompt v3 only: the fields the model says it is unsure about
    if unsure:
        reasons.append(f"{UNSURE_PREFIX}{', '.join(unsure)}")
    return {**record, "needs_review": bool(reasons), "review_reasons": reasons}


def extract_file(path, client=None):
    """Every problem with one document comes back as a record, so a batch can keep going."""
    path = Path(path)
    try:
        return run_extraction(path, client)
    except UnreadableFile as e:
        print(f"{path.name}: unreadable file ({e})", file=sys.stderr)  # the reason goes to stderr, the record stays exact
        return error_record("unreadable_file")
    except TooManyPages as e:
        return error_record("too_many_pages", str(e))


# ---- command line -------------------------------------------------------------------------

def run_batch(folders, out_dir, resume=False):
    files = sorted(p for folder in folders for p in Path(folder).iterdir() if p.suffix.lower() in SUPPORTED)
    if not files:
        sys.exit(f"no PDF or image files found in: {', '.join(map(str, folders))}")
    out_dir.mkdir(parents=True, exist_ok=True)
    client = make_client()
    counts = Counter()
    for path in files:
        target = out_dir / f"{path.stem}.json"
        # A free-tier daily cap can stop a batch part-way. With --resume, a document whose output already holds an
        # answer (a wrong one too, and "unreadable_file" is an answer) is kept; only missing or never-answered ones run.
        if resume and target.exists() and json.loads(target.read_text(encoding="utf-8")).get("error") not in NO_ANSWER_ERRORS:
            counts["kept"] += 1
            print(f"{path.name}: kept from an earlier run", file=sys.stderr)
            continue
        record = extract_file(path, client)
        target.write_text(json.dumps(record, indent=2), encoding="utf-8")
        outcome = record.get("error") or ("needs_review" if record["needs_review"] else "ok")
        counts[outcome] += 1
        print(f"{path.name}: {outcome}", file=sys.stderr)
    print(f"done: {dict(counts)}. This run cost ${spent(RUN_ID):.4f} at list price.", file=sys.stderr)
    return 0  # per-file errors are in their records; the batch itself finished


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract structured invoice data from a PDF or image.")
    parser.add_argument("paths", nargs="+", help="one file, or with --batch one or more folders")
    parser.add_argument("--batch", action="store_true", help="process every PDF/image in the folders, write <out>/<name>.json")
    parser.add_argument("--out", default=str(HERE / "outputs"), help="batch output folder (default: outputs/)")
    parser.add_argument("--resume", action="store_true", help="with --batch: keep outputs that already hold an answer, run only the rest")
    args = parser.parse_args(argv)
    if args.batch:
        return run_batch(args.paths, Path(args.out), args.resume)
    if len(args.paths) != 1:
        parser.error("give exactly one file, or use --batch with folders")
    path = Path(args.paths[0])
    if not path.is_file():
        sys.exit(f"no such file: {path}")
    record = extract_file(path)
    print(json.dumps(record, indent=2))
    return 1 if record.get("status") == "error" else 0  # non-zero so a script notices a failed document


if __name__ == "__main__":
    sys.exit(main())
