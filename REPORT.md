# Invoice extraction: report

Setup, run steps and the bad-input choices are in `README.md`. The full experiment log, including what did not help, is in `experiments.md`. Examples below use document IDs and error sizes only, because the sample pack is confidential.

## 1. Approach

**Model: `qwen/qwen3.8-27b` on Groq.** I chose it because it was the only model I could use that accepts images: I had a free Groq key and no Anthropic key, and of the 11 models on that key it is the only one with image input (checked against the live model list). That is a choice by availability, not by comparison. I tried no other model, so nothing here says how it compares to others. List price is $0.80 per million input tokens and $4.00 per million output tokens.

**Input.** Groq has no PDF input, so each PDF page is rendered to a PNG at 150 DPI and sent as an image (JPG and PNG files are sent as they are). The model accepts 3 images per request, so a PDF over 3 pages returns an error record instead of dropping pages.

**Output and validation.** The model replies with one JSON object (JSON mode). Pydantic validates it, and code enforces what a schema cannot: a real `YYYY-MM-DD` date, a three-letter currency, null in every other field for a non-invoice, no null field on an invoice (line-item `quantity` may be null), and "line amounts + tax = total" within 0.05. If a check fails the model gets one retry that shows it its own reply and the exact failure. If it still fails, the better attempt is returned with `needs_review: true` and the reasons.

**Prompting.** Prompt v1 was written from the output schema, the assignment brief and the labelling conventions in the sample pack's README (they define the task and contain no document content), with no labelled-document content and only made-up examples. It was run on the labelled set once before any failure was read. The failure analysis (section 3) led to one revision, v2, which changes only how tax is requested: the model lists each printed tax amount and the code adds them. That is the first of the two revisions I allowed myself. The second, v3, only adds a request for the fields the model is unsure about; it was used for the bonus experiment in section 5 and is not used for the outputs.

**One thing that did not work: forcing the schema with a tool call.** The plan was to force the output through a tool call so it always parses. On this model that was unreliable. On a heavily skewed photo the tool call returned an HTTP 400 because the model wrote the `line_items` array as a string and left out a field, and strict `json_schema` mode returned a 400 on the same file. A plain JSON mode reply worked on a rotated scan. That is 3 outcomes on 2 unlabelled files, thin evidence, so I switched to JSON mode plus validation plus the retry, and I do not claim the tool call can never work. A second thing that did not work: my first guard against truncated scans looked for text, images or drawings on a page, and a unit test showed a truncated scan still passed it; I replaced it with a check on the rendered pixels (see `experiments.md`).

## 2. Accuracy on the 10 labelled documents

Matching rules, fixed before any score existed (they are at the top of `eval.py`):

- `vendor`: lowercase, punctuation and company suffixes (pvt, ltd, llp, inc, plus private, limited, llc, co, corp, gmbh, bv, ag, plc) removed, `&` read as "and"; a match if equal or the rapidfuzz ratio is at least 90.
- `invoice_number`: exact after trimming and uppercasing.
- `invoice_date`, `currency`, `is_invoice`: exact.
- `tax_total`, `total`: within 0.01.
- `line_items`: each expected row is matched to an unused predicted row whose amount and quantity are within 0.01 (two nulls match) and whose description has a rapidfuzz ratio of at least 85, in any order. A document's line items count as right only if every row matches and there are no extra rows. Item-level precision and recall are pooled over all rows.
- Whole document exact: every field right. An error record gets no credit, not even for null fields.
- For the one non-invoice, a field counts as right when the model also returned null there.

| field | baseline v1 | final (v2 prompt, tolerance 0.05) |
|---|---|---|
| is_invoice | 10 / 10 | 10 / 10 |
| vendor | 10 / 10 | 10 / 10 |
| invoice_number | 10 / 10 | 10 / 10 |
| invoice_date | 10 / 10 | 10 / 10 |
| currency | 10 / 10 | 10 / 10 |
| line items (all rows right) | 10 / 10 | 10 / 10 |
| tax_total | 9 / 10 | 10 / 10 |
| total | 10 / 10 | 10 / 10 |
| whole document exact | 9 / 10 | 10 / 10 |
| line items, precision / recall | 100% / 100% (66 / 66 rows) | 100% / 100% (66 / 66 rows) |

**The final column was tuned on the same 10 documents, so it is optimistic.** Both changes behind it (the tolerance and prompt v2) came from a failure on these documents. The 10 labelled documents are the only ones I could score, and they are easy: 9 invoices and 1 non-invoice, 8 in INR, 1 GBP, 1 USD, no heavily degraded scan. 100% here says little about unseen documents.

Notes on how the baseline was obtained: the first run (`results/baseline_v1.json`) scored 70% on every field because three documents (L06, L09, L10) never got an answer: the free tier's rate limit outlasted the retries, and an error record gets no credit. The seven documents that did get an answer were all exactly right. With the user's approval I re-asked only those three, with the unchanged v1 prompt, keeping all seven earlier answers (`--resume`, `results/baseline_v1b.json`); the table above is that combined baseline. The final column is `results/expB_prompt_v2_complete.json`, where one document (L07) was resumed the same way. The submitted labelled outputs in `outputs/` come from a later full run of the final configuration, and they score the same (`results/final_outputs.json`, scored with no model calls). They are in fact identical, dict for dict, to the earlier run's predictions, with identical token counts, so at temperature 0 the model reproduced itself exactly here; that is a reproducibility check on one setup, not a second independent sample. Each configuration was run once, and one document is 10 points.

## 3. Failure analysis

After the baseline score had been shown, I read the failures. One document failed: L09, `tax_total` off by exactly 1.00; every other field of every document was right.

| # | cause | examples | caught by the pipeline? |
|---|---|---|---|
| 1 | The free tier's rate limit outlasted the retries (infrastructure, not the model) | L06, L09, L10 in the first run; L07 in a later run; U09 in an unlabelled check | yes, as error records, then fixed |
| 2 | Addition slip: when an invoice prints several tax lines and no combined figure, the model writes `tax_total` in one pass and mis-adds | L09 (final answer off by 1.00), L10 (first attempt off by 1,000), and two unlabelled invoices (off by 6.00 with three tax lines, 0.40 with two) | L10 yes, L09 and the unlabelled two with the original tolerance: no |
| 3 | Detection gap: the totals check allowed 0.1% of the total, so small errors passed | L09 (an error of 1.00 on a total in the tens of thousands, where the tolerance was several tens); the two unlabelled invoices | no |

**Reading versus adding.** Asked outside the pipeline to list the printed tax lines first and then add them, the model read the tax lines of L09 and L10 correctly and added them correctly. So the digits are read fine and the slip comes from producing the sum as one number in one pass (one trial each, so suggestive, not proof; a one-digit misread would give the same wrong totals).

**Not specific to the labelled set.** Without using any labels, the unlabelled outputs the pipeline had accepted were checked only for "line amounts + tax = total": of 6 invoices, 4 balanced exactly and 2 did not. The two were exactly the invoices with several tax lines, so those outputs held an error the original tolerance let through.

**What I fixed first, in this order, each evaluated on its own:**

1. *Detection.* Totals tolerance from 0.1% of the total to one absolute 0.05 (the ground truth of all 10 labelled documents balances exactly). With it, the checks flagged both L09 and L10 on the first attempt and the retry repaired both (12 model calls, 2 retries).
2. *Cause.* Prompt v2: the model lists the tax amounts and code adds them. L09 and L10 were right on the first attempt (10 calls, 0 retries). The unlabelled invoices with several tax lines also balanced on the first attempt, where under v1 both needed the retry.
3. *Infrastructure.* Waits of 15, 30 and 60 seconds on a rate limit, calls started at least 30 seconds apart, and a clean stop with a message on the free tier's daily token cap.

**What I would fix next:** the totals check only sees arithmetic, so an error that keeps the totals balanced (a wrong vendor, date or quantity, or two offsetting errors) is invisible to it; a second check or a model-reported confidence would be the next step. A paid or higher-limit key would also remove the throttling that made every eval slow and cost me a full day's free quota. And more labelled documents, especially non-invoices, EUR, and degraded scans.

## 4. Cost

Formula, from the token counts in each API response, summed over every call including retries:

`cost = (input_tokens x 0.80 + output_tokens x 4.00) / 1,000,000` dollars.

Price: $0.80 per million input tokens and $4.00 per million output tokens, from Groq's model page for `qwen/qwen3.8-27b` and the `pricing` field of Groq's models API, both read on 2026-11-20. The key I used is on the free tier, so I paid nothing; the figures are list-price estimates of what a paid run would cost. The log is `results/cost_log.jsonl`.

| scope | documents | model calls | input tokens | output tokens | cost | average per document |
|---|---|---|---|---|---|---|
| Labelled 10, final configuration (v2, 0.05) | 10 | 10 (0 retries) | 27,364 | 4,100 | $0.0383 | **$0.0038** (about 2,736 in and 410 out) |
| Labelled 10, final run behind `outputs/` | 10 | 10 (0 retries) | 27,364 | 4,100 | $0.0383 | $0.0038 (identical to the row above: same outputs) |
| Unlabelled 10 (U01 to U10), final run behind `outputs/` | 10 | 11 (1 retry, on U02) | 28,569 | 2,636 | $0.0334 | $0.0033 (about 2,857 in and 264 out) |
| **All 20 documents, final run behind `outputs/`** | 20 | 21 (1 retry, on U02) | 55,933 | 6,736 | $0.0717 | **$0.0036** (about 2,797 in and 337 out) |

For comparison: the baseline took 11 calls (1 retry) and $0.0408, and experiment A took 12 calls (2 retries) and $0.0445 for the same 10 documents. One page image costs a fixed amount of input (every single-page document used the same 2,468 input tokens), so cost differs between documents mostly through output length and retries. The figures leave out a few diagnostic calls made outside the pipeline (roughly $0.02 together) and any failed (429) requests, which return no usage. Note that the free tier's daily limit appears to charge about 5,100 tokens per call, roughly 1.7 times the usage the API returns (about 3,000); I inferred this from the limiter's own arithmetic and could not confirm it. It does not affect the cost figures above, which use the returned usage.

## 5. Bonus: the `needs_review` flag, and a model-reported-confidence experiment

In the shipped pipeline the flag is set when a check still fails after the retry. The other source the brief mentions, the model's own doubt about a field, is tested here as an experiment only: prompt v3 (not used for `outputs/`) asks the model to name the fields it is unsure about, and a named field flags the document without causing a retry.

Two ways to read it on the 10 labelled documents, using the saved results and the cost log:

| configuration | docs with a wrong final field | flagged | first attempts that were wrong | caught by the checks (retried) | missed |
|---|---|---|---|---|---|
| baseline v1 (tolerance 0.1%) | 1 (L09) | 0 | 2 (L09, L10) | 1 (L10) | 1 (L09) |
| experiment A (tolerance 0.05) | 0 | 0 | 2 (L09, L10) | 2 | 0 |
| experiment B (prompt v2) | 0 | 0 | 0 | none needed | 0 |

- **On the final flag, the way the brief asks:** of the documents with at least one wrong field, the share that were flagged was 0 of 1 for the baseline (L09 slipped through), and the later configurations have no wrong document left to flag. False alarms: none, because nothing was flagged. With one wrong document this says almost nothing.
- **At the first attempt, which is the better test of the checks:** a failed check implies a wrong value, because the ground truth of all 10 documents balances exactly, so the checks cannot raise a false alarm here by construction. The original tolerance caught 1 of 2 wrong first attempts; the 0.05 tolerance caught 2 of 2. Two unlabelled invoices (U01, U08) behave the same way: flagged on the first attempt by the tighter check and repaired by the retry.
- Error records (unreadable file, rate limit) are always flagged: in the first run all 3 unanswered documents were.

### The experiment: damaged copies of the labelled documents

After the fixes the labelled set has almost no mistakes left, so there is little to find. To create some, I made one damaged copy of each single-page labelled invoice: 8 documents (L01, L02, L03, L05, L06, L07, L09, L10), made by `degrade.py` (PDF pages rendered at 42 DPI instead of 150, images four times smaller in area, saved as JPEG at quality 15). A copy has the same answers as its original. The design, the measures, and the rule that it only counts as informative with at least 3 wrong documents were written down in `experiments.md` before the run. One run, prompt v3, no retry (so first attempts are judged), tolerance 0.05.

| measure | result |
|---|---|
| copies with at least one wrong field | 8 of 8 |
| flagged by a failed check | 4 of 8 (L02, L03, L06, L09) |
| flagged by the model's own doubt | 6 of 8 (L01, L02, L03, L06, L07, L09) |
| flagged by either | 6 of 8 |
| flagged only because the reply was an error record (L10, no valid JSON) | 1 of 8 |
| wrong and not flagged at all | 1 of 8 (L05) |
| fields the model doubted that were really wrong | 22 of 22 |
| wrong fields the model doubted | 22 of 48 |

What it shows:

- **At this level the model does not misread, it invents.** Every copy came back wrong, and 0 of the 42 expected line-item rows were found: the vendor, invoice number, dates, line items and totals were made up (plausible and well formed). Only 7 of 8 were still called invoices and 4 of 8 had the right vendor.
- **The checks catch an invention only when the made-up numbers fail to add up:** 4 of 8. L01, L07 and L05 balanced.
- **The model's own doubt found two documents the checks could not (L01, L07),** and every field it named was really wrong (22 of 22). But it named under half of the wrong fields (22 of 48) and missed L05 entirely, a confident, self-consistent invention that drew no failed check and no doubt, so nothing flagged it.
- **What it does not show: false alarms.** No copy stayed right, so a false alarm could not occur, and the 22 of 22 says nothing about how often the doubt fires on clean documents.
- **The damage was too severe for the question I meant to ask.** It measures how well fabrication is detected, not digit-level misreads, and a milder level would be needed to get a false-alarm rate. I did not run one, for lack of time. It is one run on synthetic damage.

## 6. Outputs for all 20 documents

`outputs/` holds one JSON per document, from the final configuration (prompt v2, tolerance 0.05). All 20 are valid records: none is an error record, none is flagged `needs_review`, and every invoice balances within 0.05. The three non-invoices (L08, and the unlabelled U06, a bank statement, and U10, a cover letter) came out as non-invoices. One document needed its retry: U02, the heavily skewed photo, which failed one check on its first attempt and passed after the retry. The 10 labelled outputs are the ones scored in section 2 (100% on every field); the 10 unlabelled outputs have no answers here, so they were only checked for internal consistency, not for accuracy.

## 7. Limitations

- **n = 10, one run per configuration.** One document is 10 points. Re-running the final configuration reproduced all 10 outputs exactly, which suggests little run-to-run noise at temperature 0, but I checked that once, on one setup.
- **The labelled set is easy and narrow:** 1 non-invoice (so `is_invoice` rests on one negative example), 8 of 10 in INR, none in EUR, no European number formats, no heavily skewed or degraded scan. The unlabelled set contains those cases, but I hold no answers for it, so I can only check it for internal consistency, not accuracy.
- **Tuned on the same 10 documents:** the final configuration's labelled score is optimistic by construction.
- **One model, chosen by availability,** and a free tier that limits throughput (about 2 documents a minute and a daily token cap that allows about 39 documents a day), so a rerun is slow and every extra experiment spends quota.
- **The checks see arithmetic only.** They cannot detect an error that keeps "line amounts + tax = total" true. The 0.05 tolerance may also flag legitimate invoices with rounding lines, which this pack does not contain.
- **Page limits and rendering:** at most 3 pages per document, and a fixed 150 DPI render.
- **A confident, self-consistent invention passes everything.** In the damaged-copy experiment, L05 balanced and drew no doubt, so neither the checks nor the model's doubt flagged it. When an image is unreadable the pipeline cannot reliably tell an invented invoice from a real one.
- **The damaged-copy experiment is one run at one severe level on synthetic damage.** It cannot say how often the flags fire on clean documents or on subtle misreads.
- **The pack is synthetic and small,** so real-world invoices may behave differently.
