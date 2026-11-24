# Invoice extraction pipeline

Reads an invoice (PDF or image) and returns validated JSON: vendor, invoice number, date, currency, line items, tax total and total. A document that is not an invoice returns `is_invoice: false` and nulls. Every result is checked in code (schema, ISO date, currency code, and "line amounts + tax = total"). A failed check triggers one retry with the exact problem fed back, and if it still fails the record comes back flagged `needs_review`.

Model: `qwen/qwen3.8-27b` on Groq, because it is the only model on a free Groq key that accepts images. Groq has no PDF input, so each PDF page is rendered to an image first.

## Setup

Needs Python 3.11 or newer and a free Groq API key from https://console.groq.com/keys.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # Windows PowerShell: Copy-Item .env.example .env
```

Then open `.env` and put your key after `GROQ_API_KEY=`. `.env` is git-ignored; never commit it.

Optional settings in `.env`: `MODEL` (default `qwen/qwen3.8-27b`), `PROMPT_FILE` (default `prompts/extract_v2.md`; `extract_v1.md` is the first version, kept to reproduce the baseline), `SPEND_CAP_USD` (default `1.5`), `SECONDS_BETWEEN_CALLS` (default `30`, see below).

### Sample pack

The sample documents are confidential and are not in this repo. Put them in `data/` so you have:

```
data/labelled/L01.pdf, L01.json, ...      documents with ground truth
data/unlabelled/U01.pdf, ...              documents without answers
data/schema.json                          the output schema (used by one test)
```

`data/` is git-ignored.

## Run

One file. The JSON goes to stdout, any messages go to stderr:

```bash
python extract.py data/unlabelled/U01.pdf > out.json
```

In Windows PowerShell 5.1, `>` writes UTF-16, which JSON readers reject. Use batch mode, PowerShell 7, or `python extract.py file.pdf | Out-File -Encoding utf8 out.json`.

All documents in one or more folders, one file per document in `outputs/` (name = file name without extension):

```bash
python extract.py --batch data/labelled data/unlabelled
```

Only `.pdf`, `.jpg`, `.jpeg` and `.png` files are picked up (the `.json` ground-truth files next to the labelled documents are ignored). `--out DIR` changes the output folder. `--resume` keeps outputs that already hold an answer (a wrong answer too, and `unreadable_file` counts as an answer) and runs only the documents that are missing or never got an answer, which is how to finish a batch that a rate limit or the daily cap stopped:

```bash
python extract.py --batch data/labelled data/unlabelled --resume
```

The free tier limits input and output tokens per minute (about 8,000 in total), which allows roughly 2 documents per minute. So the program starts model calls at least 30 seconds apart (`SECONDS_BETWEEN_CALLS`; set it to 0 on a paid tier) and still waits out any rate limit it hits, printing a line when it does. A batch of 20 documents takes about 10 minutes. The free tier also caps tokens per day (200,000 on the key used here, refilled continuously at about 2.3 tokens a second; a document is charged about 5,100, roughly 1.7 times the usage the API reports): that is about 39 documents a day, and evals and experiments share it. When the cap is hit the program stops with a message saying when to try again, instead of writing error records. Run the same batch command again later with `--resume` to finish it.

Tests (no API calls, no key needed): `python -m pytest`

## Evaluate

One command runs the extractor on the 10 documents in `data/labelled/`, compares each result to its `.json` answer field by field, and prints a table of per-field accuracy plus, for every failing document, what was expected and what came back:

```bash
python eval.py
```

This calls the model, so it needs the key and takes several minutes on the free tier. The matching rules are at the top of `eval.py`: vendor names are compared after dropping case, punctuation and company suffixes (match if equal or fuzzy ratio >= 90), invoice numbers after trimming and uppercasing, dates, currency and `is_invoice` exactly, `tax_total`/`total` within 0.01, and line items by fuzzy description plus quantity plus amount (within 0.01), reported as item-level precision and recall. "Whole document exact" means every field is right.

Useful options:

```bash
python eval.py --name baseline_v1 --no-diffs   # save under that name, print the scores only
python eval.py --show baseline_v1              # print a saved run again, failures included
python eval.py --predictions some_folder       # score existing <name>.json files, no model calls
python eval.py --resume baseline_v1 --name v1b # keep a saved run's answers, re-ask only documents that got none
```

`--resume` exists for runs cut short by rate limits or timeouts. It keeps every answer the saved run obtained, wrong ones included, and asks the model again only for documents whose result was a rate-limit, timeout or server-unavailable error. It never re-rolls an answer that was given.

Each run saves `results/<name>.json` (counts and pass/fail flags only, safe to commit), `results/<name>.txt` (the table) and `results/details/<name>.json` (expected versus returned values from the confidential pack, git-ignored). A saved name is never overwritten, so a baseline is run once. Without `--name`, the name is a timestamp.

## Review-flag experiment (bonus)

The `needs_review` flag in the outputs comes from the checks. As an experiment only, `prompts/extract_v3.md` also asks the model to name the fields it is unsure about, and `degrade.py` makes damaged copies of the labelled documents so there are mistakes to find. The design and the measures are written down in `experiments.md` before any run.

```bash
python degrade.py                                  # writes data/degraded/ (damaged copies and their answers)
PROMPT_FILE=prompts/extract_v3.md ATTEMPTS=1 python eval.py --labelled data/degraded --name expC_confidence_degraded
```

In PowerShell set the variables with `$env:PROMPT_FILE="prompts/extract_v3.md"; $env:ATTEMPTS="1"` first, and remove them afterwards with `Remove-Item Env:PROMPT_FILE, Env:ATTEMPTS`. `ATTEMPTS=1` turns the retry off so the first attempts are judged. The eval prints a "review flags" block: how many wrong documents each signal caught and how many false alarms it raised.

On a free key whose daily cap may stop the run part-way, run it as a resumable batch and score the saved predictions afterwards (no model calls):

```bash
PROMPT_FILE=prompts/extract_v3.md ATTEMPTS=1 python extract.py --batch data/degraded --out results/details/expC_predictions --resume
python eval.py --labelled data/degraded --predictions results/details/expC_predictions --name expC_confidence_degraded
```

## Output

The eight schema fields, plus two extra keys: `needs_review` (true when a check still failed after the retry) and `review_reasons` (what failed).

```json
{
  "is_invoice": true, "vendor": "Acme Supplies Pvt Ltd", "invoice_number": "INV-2041",
  "invoice_date": "2026-03-14", "currency": "INR",
  "line_items": [{"description": "A4 paper, 500 sheets", "quantity": 10.0, "amount": 3200.0}],
  "tax_total": 576.0, "total": 3776.0,
  "needs_review": false, "review_reasons": []
}
```

Rules enforced in code, not just in the prompt: date must be `YYYY-MM-DD` and real, currency must be three capital letters, a non-invoice has null in every other field, an invoice has no null field (line-item `quantity` may be null when a row has none), and the line amounts plus `tax_total` must equal `total` to within 0.05 (one absolute tolerance, `TOTALS_TOLERANCE` in `extract.py`; an earlier percentage-based tolerance hid small errors, see `experiments.md`). The prompt asks the model for each printed tax amount as `tax_lines` and the code adds them into `tax_total`, because the model read tax lines correctly but added them up wrongly.

## Bad inputs: what happens and why

| Input | Result |
|---|---|
| Not an invoice (letter, statement, purchase order) | `is_invoice: false`, all other fields null. |
| **Empty, corrupt or unreadable file** (my chosen extra case) | `{"status": "error", "error": "unreadable_file", "needs_review": true}`. In single-file mode the exit code is 1; in batch mode the batch carries on and the record is written like any other. |
| API timeout, rate limit (429) or server error | The SDK retries quickly, with exponential backoff, waiting as long as a `Retry-After` header says (up to 60 s). A 429 is then waited out for 15 s, 30 s and 60 s, one more try after each, because free-tier token limits last up to a minute and the output-token limit sends no `Retry-After`. If that runs out: `{"status": "error", "error": "api_timeout" / "rate_limited" / "api_unavailable", "needs_review": true}`. |
| PDF with more than 3 pages | `{"status": "error", "error": "too_many_pages", ...}`. The model accepts 3 images per request, so pages are refused rather than silently dropped. |
| A check fails | One retry with the failure fed back. If it still fails, the better attempt is returned with `needs_review: true`. |

How "unreadable" is detected, in two layers:

1. Before any API call: a zero-byte file, a file that is not a PDF or image (by extension or contents), a PDF with no pages, or a PDF whose every page renders as one flat colour (a truncated scan opens as a blank page, and sending that would turn damage into a confident "not an invoice").
2. After the call: the API answering "invalid image data" is also mapped to `unreadable_file`. This catches a truncated JPEG, which decodes locally (the missing part is grey) but is rejected by the API.

Known limits: a damaged file that still decodes into partly valid content goes to the model, and the null-field and totals checks flag the result for review instead. If the retry attempt fails on an API error after a first attempt worked, the first attempt is returned with `needs_review: true` and the reason. Problems with the whole run (wrong or missing key, unknown model) stop the program instead of becoming per-document records, since every document would fail the same way.

## Cost

Every API call is logged to `results/cost_log.jsonl` from the token counts the API reports, retries included. The free tier charges nothing; the log holds list-price dollars ($0.80 per million input tokens, $4.00 per million output tokens, from Groq's model page, read 2026-11-20). The program stops before any call once the logged total reaches `SPEND_CAP_USD`.
