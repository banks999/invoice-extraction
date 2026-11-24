"""Eval harness: runs the extractor on the labelled documents and scores it against the ground truth.

    python eval.py                                   run the model, print the table and the failures
    python eval.py --name baseline_v1 --no-diffs     save as results/baseline_v1.*, print the scores only
    python eval.py --predictions DIR                 score existing JSON files instead of calling the model
    python eval.py --show baseline_v1                print a saved run again, failures included
    python eval.py --resume baseline_v1 --name v1b   keep a saved run's answers, re-ask only documents that got none
"""
import argparse
import json
import re
import sys
from pathlib import Path

from rapidfuzz import fuzz

import extract

HERE = Path(__file__).parent
LABELLED = HERE / "data" / "labelled"
RESULTS = HERE / "results"

# ---- matching rules (written up in REPORT.md) -------------------------------------------------
VENDOR_MIN = 90            # rapidfuzz ratio between normalised vendor names
ITEM_DESCRIPTION_MIN = 85  # rapidfuzz ratio between normalised line-item descriptions
AMOUNT_TOLERANCE = 0.01    # tax_total, total, and each line item's amount and quantity
# Legal-form words are dropped from vendor names before comparing ("Acme Pvt Ltd" == "Acme").
COMPANY_SUFFIXES = {"pvt", "private", "ltd", "limited", "llp", "llc", "inc", "co", "corp", "gmbh", "bv", "ag", "plc"}

FIELDS = list(extract.Invoice.model_fields)  # is_invoice, vendor, invoice_number, ... in the brief's order

NO_ANSWER_ERRORS = extract.NO_ANSWER_ERRORS  # "no answer was ever obtained": safe to ask again


def close(got, expected):
    """Equal within AMOUNT_TOLERANCE; two nulls match, a null against a number does not."""
    if got is None or expected is None:
        return got is expected
    try:
        return abs(got - expected) <= AMOUNT_TOLERANCE + 1e-9  # the 1e-9 absorbs float noise at exactly 0.01
    except TypeError:  # the model returned something that is not a number
        return False


def same(got, expected):
    return got == expected


def same_number(got, expected):
    clean = lambda v: None if v is None else str(v).strip().upper()
    return clean(got) == clean(expected)


def normalise_vendor(name):
    name = name.lower().replace("&", " and ")
    words = re.sub(r"[^a-z0-9 ]", " ", re.sub(r"[.']", "", name)).split()  # "B.V." -> "bv", "Pvt." -> "pvt"
    return " ".join(w for w in words if w not in COMPANY_SUFFIXES)


def same_vendor(got, expected):
    if got is None or expected is None:
        return got is expected
    a, b = normalise_vendor(str(got)), normalise_vendor(str(expected))
    return a == b or fuzz.ratio(a, b) >= VENDOR_MIN


def normalise_description(text):
    return " ".join(re.sub(r"[^a-z0-9]", " ", str(text or "").lower()).split())


MATCHERS = {
    "is_invoice": same, "vendor": same_vendor, "invoice_number": same_number,
    "invoice_date": same, "currency": same, "tax_total": close, "total": close,
}


def match_items(expected, predicted):
    """Greedy: each expected row takes the best-matching unused predicted row. Returns (matched, missing, extra).

    A predicted row matches when its amount and quantity are within tolerance AND its description is similar.
    Order does not matter. Greedy rather than optimal, which is fine for invoices of a few dozen rows.
    """
    unused = list(range(len(predicted)))
    matched, missing = 0, []
    for e in expected:
        best, best_score = None, -1
        for i in unused:
            p = predicted[i]
            if close(p.get("amount"), e.get("amount")) and close(p.get("quantity"), e.get("quantity")):
                score = fuzz.ratio(normalise_description(p.get("description")), normalise_description(e.get("description")))
                if score >= ITEM_DESCRIPTION_MIN and score > best_score:
                    best, best_score = i, score
        if best is None:
            missing.append(e)
        else:
            unused.remove(best)
            matched += 1
    return matched, missing, [predicted[i] for i in unused]


def usable_items(value):
    """The model's line_items as a list of dicts; anything malformed counts as no items."""
    return [i for i in value if isinstance(i, dict)] if isinstance(value, list) else []


def score_document(pred, truth):
    """-> {"fields": {name: bool}, "items": {...counts}, "exact": bool, "diff": [text lines]}"""
    failed = pred.get("status") == "error"  # an error record gets no credit, not even for null fields
    fields, diff = {}, []
    for name in FIELDS:
        if name == "line_items":
            continue
        fields[name] = (not failed) and MATCHERS[name](pred.get(name), truth[name])
        if not fields[name]:
            diff.append(f"{name}: expected {ascii(truth[name])}, got {ascii(pred.get(name))}")
    expected_items = truth["line_items"] or []
    predicted_items = [] if failed else usable_items(pred.get("line_items"))
    matched, missing, extra = match_items(expected_items, predicted_items)
    same_nullness = (truth["line_items"] is None) == (pred.get("line_items") is None)
    fields["line_items"] = (not failed) and same_nullness and not missing and not extra
    if not fields["line_items"] and not failed:
        diff.append(f"line_items: matched {matched} of {len(expected_items)} expected, {len(predicted_items)} predicted")
        diff += [f"    missing: {ascii(m)}" for m in missing] + [f"    extra:   {ascii(x)}" for x in extra]
    if failed:
        diff = [f"the extractor returned an error record: {pred.get('error')}"]
    fields = {name: fields[name] for name in FIELDS}  # keep the brief's order
    items = {"matched": matched, "predicted": len(predicted_items), "expected": len(expected_items)}
    return {"fields": fields, "items": items, "exact": all(fields.values()), "diff": diff}


# ---- results ------------------------------------------------------------------------------------

def review_detail(prediction):
    """Why a record was flagged, as counts and field names only (the reason texts can hold document values)."""
    reasons = prediction.get("review_reasons") or []
    unsure = [f for r in reasons if r.startswith(extract.UNSURE_PREFIX) for f in r[len(extract.UNSURE_PREFIX):].split(", ")]
    return {"failed_checks": sum(not r.startswith(extract.UNSURE_PREFIX) for r in reasons), "unsure": unsure}


def flag_summary(public):
    """How well the review flags found the wrong documents and fields. Empty for runs saved before these keys existed."""
    docs = public["per_document"]
    if not any("failed_checks" in d for d in docs.values()):
        return []
    wrong = {name for name, d in docs.items() if not d["exact"]}
    by_check = {name for name, d in docs.items() if d["failed_checks"]}
    by_doubt = {name for name, d in docs.items() if d["unsure"]}
    lines = ["", f"review flags: {len(wrong)} of {len(docs)} documents have at least one wrong field"]
    for label, flagged in (("a failed check", by_check), ("the model's own doubt", by_doubt), ("either", by_check | by_doubt)):
        lines.append(f"  flagged by {label}: {len(flagged)} documents, {len(flagged & wrong)} of the {len(wrong)} wrong ones, "
                     f"{len(flagged - wrong)} false alarms")
    named = [(name, f) for name, d in docs.items() for f in d["unsure"]]
    wrong_fields = [(name, f) for name, d in docs.items() for f, ok in d["fields"].items() if not ok]
    hit = sum(1 for name, f in named if not docs[name]["fields"].get(f, True))
    seen = sum(1 for name, f in wrong_fields if f in docs[name]["unsure"])
    lines.append(f"  field level: the model doubted {len(named)} fields and {hit} of them really were wrong; "
                 f"it doubted {seen} of the {len(wrong_fields)} wrong fields")
    return lines


def build_result(scores, predictions, mode, usage):
    """Two dicts. `public` holds only counts and pass/fail flags, so it is safe to commit.
    `details` holds expected-versus-got values from the confidential pack, so it stays git-ignored."""
    n = len(scores)
    accuracy = {name: {"correct": sum(s["fields"][name] for s in scores.values()), "total": n} for name in FIELDS}
    accuracy["document_exact_match"] = {"correct": sum(s["exact"] for s in scores.values()), "total": n}
    for a in accuracy.values():
        a["accuracy"] = round(a["correct"] / n, 4)
    tp = sum(s["items"]["matched"] for s in scores.values())
    predicted = sum(s["items"]["predicted"] for s in scores.values())
    expected = sum(s["items"]["expected"] for s in scores.values())
    public = {
        "run": extract.RUN_ID, "mode": mode, "model": extract.MODEL, "prompt": extract.PROMPT_FILE.name, "documents": n,
        "accuracy": accuracy,
        "line_items_item_level": {
            "matched": tp, "predicted": predicted, "expected": expected,
            "precision": round(tp / predicted, 4) if predicted else 0.0,
            "recall": round(tp / expected, 4) if expected else 0.0,
        },
        "usage": usage,
        "per_document": {
            doc: {"exact": s["exact"], "fields": s["fields"],
                  "needs_review": predictions[doc].get("needs_review"), "error": predictions[doc].get("error"),
                  **review_detail(predictions[doc])}
            for doc, s in scores.items()
        },
    }
    details = {doc: {"prediction": predictions[doc], "diff": s["diff"]} for doc, s in scores.items()}
    return public, details


def run_usage(run):
    """Tokens and list-price cost of the model calls made under this run id (read from the cost log)."""
    rows = [json.loads(line) for line in extract.COST_LOG.read_text().splitlines()] if extract.COST_LOG.exists() else []
    rows = [r for r in rows if r.get("run") == run]
    return {
        "calls": len(rows), "retry_calls": sum(r["attempt"] == 2 for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows), "output_tokens": sum(r["output_tokens"] for r in rows),
        "cost_usd_list_price": round(sum(r["cost_usd"] for r in rows), 6),
    }


LABELS = {"line_items": "line_items (all rows right)", "document_exact_match": "whole document exact"}


def format_table(public):
    lines = [f"model {public['model']}, prompt {public['prompt']}, {public['documents']} labelled documents ({public['mode']})", "",
             f"{'field':<30}{'correct':>8}{'of':>5}{'accuracy':>10}"]
    for name, a in public["accuracy"].items():
        lines.append(f"{LABELS.get(name, name):<30}{a['correct']:>8}{a['total']:>5}{a['accuracy']:>10.1%}")
    li = public["line_items_item_level"]
    lines += ["", f"line items, item level: precision {li['precision']:.1%} ({li['matched']}/{li['predicted']} predicted rows right), "
                  f"recall {li['recall']:.1%} ({li['matched']}/{li['expected']} expected rows found)"]
    flagged = [d for d, s in public["per_document"].items() if s["needs_review"]]
    lines.append(f"flagged needs_review: {len(flagged)} of {public['documents']}")
    lines += flag_summary(public)
    if public["usage"]:
        u = public["usage"]
        lines.append(f"model calls: {u['calls']} ({u['retry_calls']} retries), tokens in/out {u['input_tokens']}/{u['output_tokens']}, "
                     f"list-price cost ${u['cost_usd_list_price']:.4f}")
    return "\n".join(lines)


def format_diffs(details):
    lines = []
    for doc, d in details.items():
        if d["diff"]:
            lines += ["", f"{doc}"] + [f"  {line}" for line in d["diff"]]
    return "\n".join(lines) if lines else "\nno failures"


# ---- inputs -------------------------------------------------------------------------------------

def load_truth(folder):
    """{document name without extension: ground-truth dict}, from the .json files next to the documents."""
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(folder).glob("*.json"))}


def run_extractor(folder, stems):
    client = extract.make_client()
    predictions = {}
    for stem in stems:
        doc = next(p for p in sorted(Path(folder).iterdir()) if p.stem == stem and p.suffix.lower() in extract.SUPPORTED)
        predictions[stem] = extract.extract_file(doc, client)
        print(f"{doc.name}: {predictions[stem].get('error') or 'done'}", file=sys.stderr)
    return predictions


def resume(name, labelled, truth):
    """Keep every answer a saved run obtained, wrong ones included, and ask the model again only for
    documents that got no answer (rate limit, timeout). Nothing that was answered is ever re-rolled."""
    saved = json.loads((RESULTS / "details" / f"{name}.json").read_text(encoding="utf-8"))
    kept = {d: v["prediction"] for d, v in saved.items() if v["prediction"].get("error") not in NO_ANSWER_ERRORS}
    todo = [d for d in truth if d not in kept]
    print(f"keeping {len(kept)} saved answers, running the model for {len(todo)}: {', '.join(todo) or 'none'}", file=sys.stderr)
    new = run_extractor(labelled, todo) if todo else {}
    new_usage = run_usage(extract.RUN_ID)
    old_usage = json.loads((RESULTS / f"{name}.json").read_text(encoding="utf-8"))["usage"] or dict.fromkeys(new_usage, 0)
    usage = {k: round(old_usage[k] + new_usage[k], 6) for k in new_usage}
    predictions = {d: kept[d] if d in kept else new[d] for d in truth}
    return predictions, f"resumed from {name}, model run again for: {', '.join(todo) or 'no documents'}", usage


def read_predictions(folder, stems):
    predictions = {}
    for stem in stems:
        path = Path(folder) / f"{stem}.json"
        if path.exists():
            predictions[stem] = json.loads(path.read_text(encoding="utf-8"))
        else:
            print(f"{path} is missing, scored as an error", file=sys.stderr)
            predictions[stem] = extract.error_record("missing_prediction")
    return predictions


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score the extractor against the labelled documents.")
    parser.add_argument("--labelled", default=str(LABELLED), help="folder with the labelled documents and their .json answers")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--predictions", help="folder of <name>.json files to score instead of calling the model")
    source.add_argument("--resume", metavar="NAME", help="start from saved run NAME: keep its answers, re-ask only documents that got none")
    parser.add_argument("--name", help="save as results/<name>.json (refuses to overwrite). Default: a timestamp")
    parser.add_argument("--no-diffs", action="store_true", help="print the scores only, not the per-document failures")
    parser.add_argument("--show", metavar="NAME", help="print the saved run results/<NAME>.json again, with its failures")
    args = parser.parse_args(argv)

    if args.show:
        public = json.loads((RESULTS / f"{args.show}.json").read_text(encoding="utf-8"))
        details = json.loads((RESULTS / "details" / f"{args.show}.json").read_text(encoding="utf-8"))
        print(format_table(public))
        if not args.no_diffs:
            print(format_diffs(details))
        return 0

    name = args.name or f"eval_{extract.RUN_ID}"
    if (RESULTS / f"{name}.json").exists():
        sys.exit(f"results/{name}.json already exists. Refusing to overwrite it: pick another --name.")
    truth = load_truth(args.labelled)
    if not truth:
        sys.exit(f"no ground-truth .json files in {args.labelled}")
    if args.predictions:
        predictions, mode, usage = read_predictions(args.predictions, truth), "scored existing predictions", None
    elif args.resume:
        predictions, mode, usage = resume(args.resume, args.labelled, truth)
    else:
        predictions, mode = run_extractor(args.labelled, truth), "ran the model"
        usage = run_usage(extract.RUN_ID)
    scores = {doc: score_document(predictions[doc], truth[doc]) for doc in truth}
    public, details = build_result(scores, predictions, mode, usage)
    if args.predictions:  # files this process did not generate: it cannot know which prompt made them
        public["prompt"] = "not recorded (predictions scored from files)"

    (RESULTS / "details").mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(public, indent=2), encoding="utf-8")
    (RESULTS / f"{name}.txt").write_text(format_table(public) + "\n", encoding="utf-8")
    (RESULTS / "details" / f"{name}.json").write_text(json.dumps(details, indent=2), encoding="utf-8")
    print(format_table(public))
    if not args.no_diffs:
        print(format_diffs(details))
    print(f"\nsaved results/{name}.json, results/{name}.txt and results/details/{name}.json (details are git-ignored)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
