"""Accuracy evaluation for the Sales Insight Agent.

Runs a diverse sample of questions against the live agent pipeline and
grades each answer against ground truth computed independently via pandas
(not derived from the agent's own chunks - a genuine, separate check).

Run with:  python eval_accuracy.py
Takes a while - each question is a real local Ollama call (roughly 1-3 min
apiece on CPU), so a full run is easily 20-40+ minutes.

Ground truth is recomputed here directly from the current dataset each run
(not hardcoded), so this stays correct as new monthly reports are added -
only the CASES list (the questions and which ground-truth figure/entity
each one should produce) needs updating if you add new question types.
"""

import re

import pandas as pd

from src.agent import answer
from src.index import get_index
from src.ingest import load_all


def _month_label(month):
    return pd.Period(month, freq="M").strftime("%B %Y")


def _rank_by_magnitude(series, n=1):
    return series.reindex(series.abs().sort_values(ascending=False).index).head(n)


def _extract_numbers(text):
    return [
        float(m.replace(",", ""))
        for m in re.findall(r"-?[\d,]+\.?\d*", text)
        if re.search(r"\d", m)
    ]


def grade_numeric(response, expected, tol=0.01):
    for n in _extract_numbers(response):
        if expected == 0:
            if abs(n) < 1:
                return True
        elif abs(n - expected) / abs(expected) < tol:
            return True
    return False


def grade_entity(response, expected_substr):
    return expected_substr.lower() in response.lower()


def build_cases(df):
    """(question, kind, expected) tuples. kind is 'numeric', 'entity', or
    'manual' (out-of-scope questions - graded by eye, not automatically,
    since "did it correctly decline" isn't a simple substring/number check)."""

    def total(month, category):
        return df[(df.month == month) & (df.category == category)]["net_sale"].sum()

    def top(category, group_col, n=1):
        return _rank_by_magnitude(
            df[df.category == category].groupby(group_col)["net_sale"].sum(), n
        )

    def top_in_month(category, month, group_col):
        sub = df[(df.category == category) & (df.month == month)]
        return _rank_by_magnitude(sub.groupby(group_col)["net_sale"].sum())

    months_sorted = sorted(df["month"].unique())
    latest_month = months_sorted[-1]
    an_earlier_month = months_sorted[len(months_sorted) // 2]
    latest_label = _month_label(latest_month)
    earlier_label = _month_label(an_earlier_month)

    cereals_by_month = df[df.category == "Cereals"].groupby("month")["net_sale"].sum()
    highest_cereals_month = _rank_by_magnitude(cereals_by_month).index[0]
    lowest_cereals_month = cereals_by_month.abs().idxmin()

    sale_type_totals = df[df.category == "Dairy"].groupby("sale_type")["net_sale"].sum()
    larger_sale_type = sale_type_totals.abs().idxmax()

    return [
        (f"What were total net sales for Dairy in {earlier_label}?", "numeric",
         total(an_earlier_month, "Dairy")),
        (f"What were total net sales for Cereals in {latest_label}?", "numeric",
         total(latest_month, "Cereals")),
        (f"Which Cereals brand performed best in {earlier_label}?", "entity",
         top_in_month("Cereals", an_earlier_month, "brand").index[0]),
        (f"Which Dairy customer had the highest sales in {latest_label}?", "entity",
         top_in_month("Dairy", latest_month, "customer_name").index[0]),
        ("Which Dairy channel generated the most sales overall?", "entity",
         top("Dairy", "channel").index[0]),
        ("What is the top-selling Dairy material overall?", "entity",
         top("Dairy", "mat_name").index[0]),
        ("Who is the top Dairy customer overall, across all months?", "entity",
         top("Dairy", "customer_name").index[0]),
        ("Which Cereals brand had the highest sales overall, across all months?", "entity",
         top("Cereals", "brand").index[0]),
        ("Which month had the highest Cereals sales overall?", "entity",
         _month_label(highest_cereals_month)),
        ("Which month had the lowest Cereals sales overall?", "entity",
         _month_label(lowest_cereals_month)),
        ("What was the total net sales for the Coated brand in Cereals across all months?",
         "numeric", df[(df.category == "Cereals") & (df.brand == "Coated")]["net_sale"].sum()),
        (f"How many unique invoices are there for Dairy in {earlier_label}?", "numeric",
         df[(df.month == an_earlier_month) & (df.category == "Dairy")]["invoice_no"].nunique()),
        (f"How many line items are in the Cereals sheet for {latest_label}?", "numeric",
         len(df[(df.month == latest_month) & (df.category == "Cereals")])),
        ("Compare Cash Sale vs Credit Sale totals in Dairy - which is larger?", "entity",
         larger_sale_type),
        ("What is the grand total across all categories and all months combined?", "numeric",
         df["net_sale"].sum()),
        ("What is the top-selling Cereals material overall?", "entity",
         top("Cereals", "mat_name").index[0]),
        ("What were total sales for the Snacks category?", "manual", None),
        ("What is the capital of France?", "manual", None),
    ]


def main():
    df = load_all()
    idx = get_index(verbose=False)
    cases = build_cases(df)
    results = []

    for i, (question, kind, expected) in enumerate(cases, 1):
        response = answer(question, idx)

        if kind == "numeric":
            passed = grade_numeric(response, expected)
        elif kind == "entity":
            passed = grade_entity(response, expected)
        else:
            passed = None  # manual review

        status = "PASS" if passed else ("FAIL" if passed is False else "MANUAL")
        results.append((i, question, status, response, expected))
        print(f"[{i}/{len(cases)}] {status}")
        print(f"  Q: {question}")
        if expected is not None:
            print(f"  Expected: {expected}")
        print(f"  A: {response[:250]}")
        print()

    graded = [r for r in results if r[2] in ("PASS", "FAIL")]
    passed_count = sum(1 for r in graded if r[2] == "PASS")
    print("=" * 60)
    print(f"AUTO-GRADED: {passed_count}/{len(graded)} passed "
          f"({passed_count / len(graded) * 100:.0f}%)")
    manual = [r for r in results if r[2] == "MANUAL"]
    if manual:
        print(f"MANUAL REVIEW NEEDED: {len(manual)} question(s) (out-of-scope checks)")
    print()
    failures = [r for r in results if r[2] == "FAIL"]
    if failures:
        print("Failures:")
        for r in failures:
            print(f"  [{r[0]}] {r[1]}")
            print(f"      expected: {r[4]}")
            print(f"      got:      {r[3]}")


if __name__ == "__main__":
    main()
