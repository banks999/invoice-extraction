# Experiments log

Running log, newest at the bottom. Every prompt change gets its score, including the ones that did not help.

## Setup facts (2026-11-20)

- Provider and model: Groq, `qwen/qwen3.8-27b`. The original plan was Anthropic `claude-haiku-5-5`, dropped because no Anthropic API key was available. A free Groq key was. Of the 11 models that key can see, this is the only one that accepts images (live `GET /openai/v1/models`, field `input_modalities`).
- List price: $0.80 per million input tokens and $4.00 per million output tokens (Groq model page and the models API, read 2026-11-20). The free tier charges nothing, so cost figures in this project are list-price estimates from real token counts. The spend cap counts the same list-price dollars.
- Free-tier limits seen on the live API: about 8,000 tokens per minute (one document uses about 2,500-3,000, so roughly 2 documents per minute) and 1,000 requests per day. A 10-document eval therefore takes several minutes. The SDK waits out 429 responses using the Retry-After header.
- No Groq model reads PDFs directly, so PDF pages are rendered to PNG (PyMuPDF, 150 DPI). The model accepts at most 3 images per request, so a PDF with more than 3 pages returns an error record instead of dropping pages.
- Only the prompt file changes between experiments (`PROMPT_FILE`), so scores stay comparable.
- Eval set: the 10 labelled documents only. It has 9 invoices and 1 non-invoice, so `is_invoice` rests on a single negative example.

## Output mode: forced tool call did not work reliably (this is the report's "did not work" item)

The brief asked for the schema to be forced with a tool call. Before building on it I tried three output modes against the live model on two unlabelled files (a heavily skewed photo and a 90-degree rotated scan), with the same prompt text:

| mode | skewed photo | rotated scan |
|---|---|---|
| forced tool call | HTTP 400 `tool_use_failed`: the model wrote `line_items` as a string and left out `invoice_date` | worked, but one line amount was misread (the totals check caught it) |
| strict `json_schema` | HTTP 400 `json_validate_failed` | not tried |
| `json_object` with the schema in the prompt | not tried (hit the rate limit) | worked, totals correct |

Decision: JSON mode, then Pydantic validation and the single retry. That is thin evidence (3 outcomes on 2 files), so the choice may not hold up. The retry loop also treats the API's own `json_validate_failed` 400 as a failed attempt rather than crashing.

These probes ran outside `extract.py`, so about 5 calls (roughly $0.01 or less at list price) are missing from `results/cost_log.jsonl`.

## Exposure note (read this before trusting any score)

Before writing prompt v1, I (the assistant) read the text and images of all 20 sample documents and structural facts of the labelled ground truth (field presence, nulls, totals), because the brief asked for a full inspection of the pack first. I have not run any eval and have seen no model output on a labelled document.

To keep v1 honest, it contains only: the output schema, the labelling conventions from the pack's README (these define the task, they are not document content), the three non-invoice examples named in the assignment brief, the made-up Acme example from the brief, and a made-up non-invoice example. It carries no hint that I only know from looking at the pack. Any such hint added later goes into a revised prompt and is logged here as tuned on the labelled set.

The only edit to v1 after it was first written was to its opening sentence and the added non-invoice example, to match JSON-mode output instead of a tool call. That was done before any eval and after the probes above, which used two unlabelled files.

Smoke test through `extract.py`: one unlabelled digital PDF, valid record, no retry, 2,468 input and 220 output tokens (about $0.003 at list price). Not scored, since unlabelled files have no ground truth here.

## Bad-input checks (Phase 2)

What did not work first: the blank-page guard originally asked whether a PDF page had text, images or drawings. A unit test with a generated truncated scan showed it still listed an image whose data was gone, so that file passed and would have reached the model as a blank page, which then answers "not an invoice" with confidence. Replaced by a check on the rendered pixels: if every page renders as one flat colour, the file is unreadable. Two real truncated scans from the pack behaved differently from the generated one (they opened as a blank page with no image listed), which is why a structural check looked fine until the test.

Live checks, using only unlabelled files and damaged copies of them, written to a scratch folder (not `outputs/`):

- Empty PDF, header-only PDF, garbage PNG, truncated scan PDF: caught before any API call.
- Truncated JPEG: decodes locally (missing part is grey), so it went to the API, which answered 400 `invalid image data`. The mapping to `unreadable_file` caught it. This is why there are two layers.
- Three real unlabelled documents in the same batch (digital PDFs, one of them a non-invoice): all returned valid records with `needs_review: false`, 52 s for the whole batch of 8 files, $0.0082 at list price. Each used the same 2,468 input tokens, so a rendered page costs a fixed amount of input.
- The exact `unreadable_file` record is returned in all five damaged cases, single-file mode exits 1, and the reason goes to stderr.

Nothing here is a score: unlabelled files have no ground truth here, and none of this was used to tune the prompt.

## Eval harness decisions (Phase 3), fixed before any labelled score existed

- Thresholds: vendor fuzzy ratio >= 90 and amount tolerance 0.01 come from the brief. The line-item description threshold (ratio >= 85) is mine, picked once, before any model output was scored. All runs use the same rules, so scores stay comparable. Changing a rule after seeing failures would be tuning the metric, so if it ever happens it is logged here.
- The vendor suffix list is wider than the brief's four examples (also private, limited, llc, co, corp, gmbh, bv, ag, plc), chosen from general knowledge of company names, not from labelled failures. None of the ten labelled vendors depends on the extra words.
- Greedy line-item matching, order-insensitive. Rows with the right amount but a wrong description, or the other way round, count as one missing and one extra row.
- A document that is not an invoice counts as correct on a field when the model also returned null there, so the one labelled non-invoice adds a trivially correct row to most fields when classified right. An error record gets no credit, not even for null fields.
- The "run once" rule is enforced in code: a saved result name is never overwritten. `--no-diffs` prints only the scores, so the baseline can be shown without anyone reading a labelled failure first.
- Result files are split for confidentiality: `results/<name>.json` has counts and pass/fail flags only, `results/details/` (git-ignored) has the values.
- Checked without any model: the ground truth scored against itself is 100% on every field (66 of 66 line-item rows), and a perturbed copy was scored exactly as hand-counted (forgiven: case and suffix in a vendor, case in an invoice number, one cent in an amount; caught: a wrong total, vendor, date, a dropped row, a non-invoice called an invoice, a missing prediction).

Second attempt at 20:20 with a new API key from the user: the key works, but it shares the daily cap with the first one (keys in the same Groq organisation draw from the same limits), so it added no quota. `--resume` kept the 14 finished outputs, finished U05 (valid, not flagged) and then hit the cap again at U06, stopping cleanly. 15 of 20 outputs are done. Cost of those 15: 16 calls (one retry, U02), 43,148 input and 5,795 output tokens, $0.0577 at list price, $0.0039 per document. Project spend so far is $0.24 of the $1.50 cap. By the same refill arithmetic, U06-U10 (about 25,500 charged tokens) can run from about 23:00, and U06-U10 plus the experiment (about 24,000 more) from about 02:00.

## Experiment C (bonus): do the review flags find the mistakes? Design fixed before any run (2026-11-23)

Chosen by the user: the model-confidence part of the bonus as an experiment only. The final outputs stay on prompt v2; nothing in `outputs/` changes.

Question: when the extractor is wrong, does it say so? Two signals are compared on the same documents: a failed check (the totals, date, currency and null checks) and the model's own doubt (a list of fields it says it is unsure about).

Data: eight damaged copies of labelled single-page invoices (L01, L02, L03, L05, L06, L07, L09, L10), made by `python degrade.py` (PDF pages rendered at 42 DPI instead of 150, images made four times smaller in area, everything saved as JPEG at quality 15). A copy has the same ground truth as its original. L04 (two pages) and L08 (not an invoice) are left out. One degradation level, chosen by looking at the copies, not tuned on any result.

Run (one run, nothing repeated): prompt v3 (`prompts/extract_v3.md`: v2 plus `low_confidence_fields`; this is the second and last of the two prompt revisions I allowed myself), totals tolerance 0.05, `ATTEMPTS=1` so there is no retry and the first attempts are judged before the retry can repair them. Procedure change, made before any run because the free key's daily cap can stop a run part-way and an eval run saves nothing when that happens: the model is run as a resumable batch, `python extract.py --batch data/degraded --out results/details/expC_predictions --resume`, and the saved predictions are then scored with no model calls by `python eval.py --labelled data/degraded --predictions results/details/expC_predictions --name expC_confidence_degraded`. The prompt, the data, the settings and the measures are unchanged. A model's doubt never triggers a retry: it is a reason to look, not a failed check.

Measures, fixed now: (1) documents with at least one wrong field; (2) for a failed check, for the model's doubt and for either, how many of those wrong documents were flagged and how many flags fell on documents with no wrong field (false alarms); (3) at field level, how many of the fields the model doubted were really wrong, and how many of the wrong fields it doubted. These are printed by the eval as "review flags".

What would make it informative: at least 3 wrong documents. With fewer, the result is reported as inconclusive and not as evidence for or against the signal.

Caveats written in advance: 8 documents and one synthetic degradation, which is not the noise of real scans; false alarms on clean documents are only seen on the copies the damage did not break (no tokens were spent on clean originals); the signal is judged on first attempts, while the shipped pipeline also retries; the expected result is unknown, and a negative result is a result.

Cost: the damaged copies are small images, so each call should use well under the usual input; about 8 calls.

### Experiment C result (2026-11-24)

Run exactly as designed (through the resumable batch, then scored with `--predictions`): all 8 copies were answered. Saved as `results/expC_confidence_degraded.json`.

The damage was far too severe. The model did not misread the copies, it invented them: vendors, invoice numbers, dates and line items were made up, plausible and well formed. All 8 copies were wrong (the pre-registered bar of at least 3 wrong documents is met, but not in the way the design meant), 0 of 42 expected line-item rows were found, per-field accuracy was is_invoice 7 of 8, vendor 4 of 8, currency 5 of 8 and 0 of 8 everywhere else, and L10 returned no valid JSON at all (an error record).

Review flags, on the pre-registered measures:

| signal | documents flagged | wrong ones caught | false alarms |
|---|---|---|---|
| a failed check (L02, L03, L06, L09) | 4 | 4 of 8 | 0 (not measurable, see below) |
| the model's own doubt (L01, L02, L03, L06, L07, L09) | 6 | 6 of 8 | 0 (not measurable) |
| either | 6 | 6 of 8 | 0 (not measurable) |

Plus L10, flagged only because an error record is always flagged, and L05 not flagged at all. L05 was a fully invented invoice that balanced and drew no doubt from the model. L01 and L07 also balanced, so the checks could not see them, but the model doubted them. Field level: the model doubted 22 fields and all 22 really were wrong (100%), and it doubted 22 of the 48 wrong fields (46%).

Read honestly: (1) checks catch an invention only when the made-up numbers fail to add up, about half of the time here; (2) the model's doubt added coverage (two documents the checks could not see) and was never wrong when it spoke, but it stayed silent on more than half of the wrong fields and missed one whole invention; (3) false alarms could not be measured at all, because no copy stayed right, so the 100% precision means nothing about clean documents; (4) I picked the degradation level by looking at two copies and judged it "hard but readable", which was wrong for the model, and I had no tokens to pilot it. A milder level (for example 90 DPI, quality 40) would be the next step to get misreads instead of inventions and a false-alarm rate; it was not run because of the submission deadline.

Two bookkeeping notes. The first score of this experiment printed the prompt as `extract_v2.md` because the scoring step cannot know which prompt generated saved predictions; the predictions were generated with `extract_v3.md`. I corrected the label in the two aggregate result files by hand (no score changed) and changed `eval.py` so scored files now record "not recorded (predictions scored from files)". The experiment used about 8 small-image calls; its cost rows are in `results/cost_log.jsonl` under prompt `extract_v3.md`.

## v1: baseline (prompt `prompts/extract_v1.md`)

Written from the schema, the brief and the pack README conventions.

### Run 1: `python eval.py --name baseline_v1 --no-diffs` (saved as `results/baseline_v1.json`)

Scores as the harness printed them, 10 labelled documents, prompt v1, model `qwen/qwen3.8-27b`:

| field | correct | accuracy |
|---|---|---|
| is_invoice, vendor, invoice_number, invoice_date, currency, line_items (all rows), tax_total, total | 7 of 10 each | 70.0% |
| whole document exact | 7 of 10 | 70.0% |

Line items, item level: precision 100.0% (52 of 52 predicted rows right), recall 78.8% (52 of 66 expected rows). 7 model calls, 0 retries, 19,070 input and 3,045 output tokens, $0.0274 at list price. No labelled failure was read: only these aggregates and the progress lines.

**This 70% is not the model's accuracy.** L06, L09 and L10 came back as `rate_limited` error records (the free tier's limit outlasted the retries), and an error record gets no credit. Every document that did get an answer (7 of 7) was exactly right on every field, which follows from the counts: 7 exact documents, 7 answered. The 14 missing rows (66 - 52) are consistent with the row counts of those three documents.

What went wrong: the free tier limits input and output tokens per minute. The output-token 429 comes with no `Retry-After` header, so the SDK fell back to 0.5-2 s backoffs and its 3 retries were over in seconds, long before the window clears (20-60 s). Diagnosed with back-to-back calls on an unlabelled file (SDK retries off): `x-ratelimit-reset-tokens` showed 23-38 s, and one 429 named an output limit of 1000 tokens per minute.

Fix (`RATE_LIMIT_WAITS` in `extract.py`): after a 429, wait 15 s, 30 s, then 60 s before giving up, on top of the SDK's own retries. Still exponential, still "up to 3 retries, then an error record". Verified live on six unlabelled documents run back-to-back (about 16,000 input tokens against an 8,000 per minute limit): all six returned records, with waits of 15 s and 30 s visible, 129 s in total. The fix does not touch the prompt or the model call, so it cannot change what the model answers.

Remedy for the baseline, pending the user's approval: keep run 1 exactly as saved, and re-run only the three documents that never got an answer, with the unchanged v1 prompt, using `python eval.py --resume baseline_v1 --name baseline_v1b`. `--resume` keeps every answer run 1 obtained (wrong ones included) and only re-asks documents whose record is a rate-limit, timeout or server-unavailable error, so no answered document can be re-rolled. The combined result would be reported as the v1 baseline, with this paragraph attached.

Not logged in `results/cost_log.jsonl`: the output-mode probes, and the one successful call of the rate-limit diagnostic (about $0.01 or less together).

### Run 2: `python eval.py --resume baseline_v1 --name baseline_v1b --no-diffs` (saved as `results/baseline_v1b.json`)

Approved by the user. Kept run 1's 7 answers, asked the model again for L06, L09 and L10 only, same v1 prompt, with the rate-limit fix. All three got an answer. **This combined run is the v1 baseline.** Run 1 stays saved as it was.

| field | correct | accuracy |
|---|---|---|
| is_invoice, vendor, invoice_number, invoice_date, currency, line_items (all rows), total | 10 of 10 each | 100.0% |
| tax_total | 9 of 10 | 90.0% |
| whole document exact | 9 of 10 | 90.0% |

Line items, item level: precision 100.0% (66 of 66), recall 100.0% (66 of 66). Flagged `needs_review`: 0 of 10. In total 11 model calls (1 retry), 29,390 input and 4,324 output tokens, $0.0408 at list price.

What the aggregates alone show, before any failure analysis:

- Exactly one document is wrong, and only on `tax_total`: every other field is 10 of 10 and exact match is 9 of 10.
- That document was not flagged. It passed every check, so the totals check accepted a `tax_total` that the harness (tolerance 0.01) counts as wrong. The check's tolerance is 0.1% of the total, so a small tax error can slip through. For the bonus, flag recall on this baseline is 0 of 1.
- The retry path ran once during this run and the totals check did catch a first attempt on L10 (see the exposure note below), which is the mechanism working as designed.

Exposure: the retry progress line used to print the text of the failed check. For L10 that text contained that document's first-attempt numbers, so I saw one first-attempt error before the user saw this score. `baseline_v1b.json` had already been saved when I read the output, so the baseline itself was not influenced. The line now prints only the number of failed checks.

Cumulative list-price spend after this run is in `results/cost_log.jsonl`; the cap is $1.50.

### Failure analysis of the v1 baseline (the first time any failure was read)

Read with `python eval.py --show baseline_v1b`, after the user had seen the score. One document fails: **L09**, `tax_total` off by exactly 1.00. Every other field of every document is right.

Errors grouped by cause:

| # | cause | documents | caught by the pipeline? |
|---|---|---|---|
| 1 | The free tier's rate limit outlasted the retries (infrastructure, not the model) | L06, L09, L10 in run 1 | yes, as error records. Fixed with longer waits |
| 2 | Single-pass addition slip: an invoice prints several tax lines (CGST and SGST) and no combined figure, and the model writes `tax_total` straight away and mis-adds | L09 (final answer, off by 1.00), L10 (first attempt, off by 1000; the retry fixed it) | L10 yes (totals check, then retry). L09 no |
| 3 | Detection gap: the totals check allows 0.1% of the total, so a small error passes | L09, and two unlabelled documents (below) | no |

Evidence for cause 2, reading versus adding: asked outside the pipeline to list the printed tax lines first and then add them, the model read both tax lines of L09 and of L10 correctly and added them correctly. So it reads the digits fine; the slip comes from producing `tax_total` as one number in one pass. One trial per document at temperature 0, so this is suggestive, not proof, and a one-digit misread of a line would give the same wrong totals.

Evidence that it generalises beyond the labelled set, with no labels used: the unlabelled outputs the pipeline had accepted (`needs_review` false), checked only for "line amounts + tax = total". Of 6 distinct invoices, 4 balance exactly and 2 do not (off by 6.00 and by 0.40). The two are exactly the ones that print several tax lines (three and two). Their printed values do balance, so those outputs contain an error that the 0.1% rule let through.

Why cause 3 matters on its own: the ground truth of all 10 labelled documents balances to 0.0000, and so do 4 of the 6 unlabelled outputs, yet the rule tolerated an error of several tens on L09's total (a total in the tens of thousands). A tolerance that scales with the total hides every error smaller than 0.1% of it, which is also why the bonus flag missed L09.

Fixes proposed (nothing applied yet, each needs the user's go):

- **A. Tighten the totals tolerance to an absolute 0.05.** It would flag exactly the three documents above (L09 and the two unlabelled) and nothing that balances. It departs from the formula given in the brief (`max(0.02, 0.001 * total)`), and it is tuned on a failure from the labelled set, so any improvement is optimistic. Not a prompt change.
- **B. Prompt v2 (revision 1 of at most 2): the model lists each printed tax amount and the code adds them**, so the addition is done by code, not by the model. A document with no tax gives an empty list, which sums to 0 as the pack's labelling rule requires.
- Order: A first (smallest change, and it also catches other small misreads), then B, each evaluated on its own so the effect of each is clear. Single-document effects at n=10 are within noise, so what each run can show is mostly whether L09 is fixed and nothing else broke.

The user approved both ("Yes, A then B").

### Experiment A: totals tolerance 0.05, prompt v1 unchanged (`results/expA_tolerance_005.json`)

Change: `TOTALS_TOLERANCE` went from `max(0.02, 0.001 * total)` to one absolute 0.05, compared after rounding to the cent (float noise: `10.0 + 0.0 - 10.05` is 0.05000000000000071). Prompt `extract_v1.md`, same model, full run of the 10 labelled documents.

| field | correct | accuracy |
|---|---|---|
| every field, and whole document exact | 10 of 10 | 100.0% |

Line items: precision 100.0% (66 of 66), recall 100.0% (66 of 66). Flagged `needs_review`: 0 of 10 (every flag that fired was cleared by the retry). 12 model calls, 2 retries, 32,309 input and 4,654 output tokens, $0.0445 at list price.

What happened (see the table of runs further down): the tighter check flagged both L09 and L10 on their first attempt, and the one retry (which is shown the exact discrepancy) corrected both. Compared with the baseline: L09 is fixed, L10 behaves as before, nothing else changed. The first attempts were very likely identical to the baseline's (a later check showed the model reproduces its outputs exactly at temperature 0, see below), so L09 changed because the tighter check forced the retry, not because of run-to-run noise. On 10 documents the honest reading is "the check now catches the slip and the retry repairs it in these two cases", not a measured accuracy gain. It is tuned on the failure it removes, so 100% here is optimistic.

### Experiment B: prompt v2, the model lists the tax amounts and code adds them (on top of A)

Change (prompt revision 1 of at most 2): `prompts/extract_v2.md` asks for `tax_lines`, one number per printed tax amount, instead of `tax_total`. `check_reply` in `extract.py` sets `tax_total = round(sum(tax_lines), 2)`; no tax is an empty list, which sums to 0 as the pack's rule requires. Replies without `tax_lines` (prompt v1) pass through unchanged, so every earlier run is reproducible with `PROMPT_FILE=prompts/extract_v1.md`. The Acme example in the prompt keeps the brief's totals but shows the tax as two lines.

First run (`results/expB_prompt_v2.json`): L07 stayed rate-limited through all three waits, so every field showed 9 of 10 (90.0%) and item recall 93.9%. The 9 documents that got an answer were all exactly right, with no retries. Same remedy as the baseline: `--resume` re-asked L07 alone (`results/expB_prompt_v2_complete.json`) and it answered on the first try.

| field | correct | accuracy |
|---|---|---|
| every field, and whole document exact | 10 of 10 | 100.0% |

Line items: precision 100.0% (66 of 66), recall 100.0% (66 of 66). Flagged `needs_review`: 0 of 10. 10 model calls, 0 retries, 27,364 input and 4,100 output tokens, $0.0383 at list price.

| run | prompt | tolerance | result on the 10 labelled documents | calls | retries | list-price cost |
|---|---|---|---|---|---|---|
| `baseline_v1b` | v1 | 0.1% of total | tax_total 9 of 10, whole document 9 of 10, rest 100% | 11 | 1 | $0.0408 |
| `expA_tolerance_005` | v1 | 0.05 | 100% everywhere | 12 | 2 | $0.0445 |
| `expB_prompt_v2_complete` | v2 | 0.05 | 100% everywhere | 10 | 0 | $0.0383 |

On the labelled set A and B end at the same score, so the score cannot tell them apart. B's visible difference is that L09 and L10 are right on the first attempt: 0 retries instead of 2, so less cost and latency, and no dependence on the retry succeeding.

Informal check on the unlabelled documents that print several tax lines (no labels, judged only by whether line amounts + tax = total), four unlabelled invoices run under each prompt with the 0.05 check:

- v1: U01 (three tax lines) and U08 (two) each failed one check on the first attempt and the retry repaired both, so both outputs balance. The single-tax and no-tax controls (U03, U09) were fine.
- v2: U01 and U08 balanced on the first attempt with no retry. U03 fine. U09 was cut off by the rate limit in this run and was not repeated.

This is the evidence that the cause is general and that v2 removes it, beyond the one labelled document. One trial per configuration, so it is suggestive.

Caveats to carry into the report: both changes came from a failure on the same 10 labelled documents, so the 100% is optimistic and says little about unseen documents; each configuration was run once (but see the reproducibility check below); with 10 documents one document is 10 points.

### What the rate limit did to the evals, and the pacing fix

Two more documents (L07 in experiment B, U09 in the unlabelled check) outlasted all three waits (15, 30, 60 s), although both answered right away when run alone later. Diagnosis on an unlabelled file: three per-minute limits apply at once (input 7,000, total 8,000 and output 1,000 tokens per minute), the reset time reads about 42 s, and on an emptied window one call succeeds at `max_tokens` 8,000, 3,000 and 1,500 alike, so `max_tokens` is not the cause. The cause is throughput: one document uses about 3,000 tokens, so more than about 2 a minute collides. Fix: `SECONDS_BETWEEN_CALLS` (default 30) starts calls at least 30 s apart so most 429s never happen; the waits stay as a backstop. A 10-document eval now takes about 5 minutes and a 20-document batch about 10. Set it to 0 on a paid tier.

### Final configuration

Prompt `prompts/extract_v2.md` (now the default), totals tolerance 0.05, pacing 30 s, model `qwen/qwen3.8-27b`. One of the two allowed prompt revisions is used. This configuration was built from failures on the labelled documents, so its labelled score is optimistic by construction.

### The free tier's daily token cap stopped the final 20-document run (2026-11-23)

The batch failed from its very first document: every call came back 429 even after all three waits. Cause: besides the per-minute limits, the free key has a daily limit of 200,000 tokens (TPD), and the API reported 197,596 already used. It refills continuously, at about 2.3 tokens a second (200,000 / 86,400; the retry-after times in the error messages match this), which is about 8,300 tokens an hour, or roughly 2.5 documents an hour. Waiting 15, 30 and 60 seconds cannot help against it. The batch was stopped and the three error-record files it had already written to `outputs/` were deleted, so `outputs/` is empty again.

Where the day's tokens went: this project's logged runs in the last 24 hours (the baseline resume, experiments A and B, the L07 resume and two unlabelled comparison runs, all within about 45 minutes) account for about 105,000 tokens, and unlogged diagnostics for roughly 17,000 more. The API reports about 75,000 more than that, which I cannot explain from my logs. One observation: the per-minute headers showed a charge of about 3,300 tokens for a call whose reported usage was 2,688, so the API appears to charge more per call than the usage it returns; the same key may also have other use. Either way, the repeated eval runs and diagnostics used up the day's quota, not the final run. Lesson: the daily cap was invisible in my earlier diagnostics because only per-minute limits showed up; I should have checked the daily budget before spending it on experiments.

Changes made: the extractor now stops at once on a daily-limit 429 with a message saying when to try again (the run stops instead of waiting and writing junk error records), with a test; the README states the daily cap.

README tested from a clean copy (2026-11-23, while waiting): the files a clone would contain were copied into a temp folder outside the project, a fresh venv was created and `pip install -r requirements.txt` run, as the README says. 108 tests passed with 1 skipped (the schema test, because `data/` is absent), an empty PDF returned the exact `unreadable_file` record with exit code 1 and no key needed, and `eval.py` without the sample pack said so clearly. One problem found: with the unedited `.env.example` copied to `.env` the placeholder key counts as set, so the request went out and failed with a raw traceback. Fixed: a 401 now stops the run with "Groq rejected the API key... put your key after GROQ_API_KEY= in .env", with a test.

Update, the same evening (19:37 to 19:45): the final batch was started again and finished 14 of 20 documents (L01-L10 and U01-U04, one retry on U02, nothing flagged), then stopped cleanly on the daily cap at U05 with "try again in about 9 minutes"; no junk records were written. Cause of the shortfall: my estimate of the charge per call was too low. Reported usage averaged 3,073 tokens per call over the 15 accepted calls, but the bucket arithmetic (200,000 refilled at 2.31 tokens a second, about 79,700 free at 19:37, and the refused request being 1,312 tokens short) implies about 5,100 tokens charged per call, roughly 1.7 times the reported usage. That also matches the earlier gap (197,596 charged against about 105,000 logged). So this key allows about 39 documents a day, not 60, and the last six documents (U05-U10) need about 3 more hours of refill. Batch mode gained `--resume` (keep outputs that already hold an answer, run only the missing or never-answered ones), with tests, so the batch can be finished without redoing the 14.

Reproducibility check: the 10 labelled outputs of this batch are identical, dict for dict, to experiment B's predictions, and the token totals match to the unit (27,364 input, 4,100 output). So at temperature 0 the model reproduced itself exactly here. It makes the batch a reproducibility check, not a second sample, and it suggests run-to-run noise is small for this setup, though I have only checked it once. The submitted labelled outputs score 100% on every field (`results/final_outputs.json`, scored with no model calls), identical to experiment B.

(Earlier estimate, kept for the record and superseded by the update above:) needed to finish about 3,000 tokens per document at actual usage, up to about 3,300 as charged, so about 30,000-33,000 for the 10 unlabelled documents and about 60,000-66,000 for all 20. At the refill rate that is roughly 3 to 4 hours of waiting for the unlabelled 10 and 7 to 8 hours for all 20, unless the key is upgraded to a paid tier.
