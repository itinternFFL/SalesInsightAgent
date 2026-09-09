"""RBAC scoping accuracy evaluation for the Sales Insight Agent.

Different from eval_accuracy.py (which checks whether the agent gives the
right NUMBERS for general questions). This script checks whether access
control is actually being respected end to end: for every real employee
in the current hierarchy, does the live chat pipeline answer correctly
when asked about their OWN data, and correctly refuse - never leaking the
real figure - when asked about data that belongs to someone outside their
accessible branch?

Ground truth (who owns which brand, and its correct total) is computed
independently via pandas straight from data/employees/<name>/, not
derived from the agent's own chunks. Expected in-scope/out-of-scope
status per (asker, target) pair is computed via the app's own
get_accessible_user_ids() - that function is unit-tested for correctness
separately in tests/test_access_control.py; what THIS script checks is
whether the chat pipeline actually respects what it says.

Nothing here is hardcoded to today's specific test employees - it reads
the live users table and each person's own data/employees/<name>/ folder,
so it stays correct as the hierarchy changes (new employees,
reassignments, etc.), the same way eval_accuracy.py's ground truth is
recomputed from the current dataset rather than hardcoded.

Run with:  python eval_rbac_accuracy.py
Tests every (asker, target) pair, including self-pairs - for N employees
that's N^2 real chat calls, each a real local Ollama generation (roughly
15-40s on this setup with qwen2.5:3b, more for broader questions). For
the current 7-person test hierarchy that's 49 calls, easily 20-30+
minutes. Pass --quick to test only self-access plus one in-scope and one
out-of-scope pair per asker instead of the full matrix - much faster,
less exhaustive about leak-checking across every possible pair.
"""

import re
import sys

import backend.db as db
from backend.access_control import employee_folder_name, get_accessible_filenames, get_accessible_user_ids
from src.agent import answer
from src.index import build_index_from_df
from src.ingest import DATA_DIR, load_all


def _extract_numbers(text):
    return [
        float(m.replace(",", ""))
        for m in re.findall(r"-?[\d,]+\.?\d*", text)
        if re.search(r"\d", m)
    ]


def number_present(text, expected, tol=0.01):
    """Does `expected` (or something within tol of it) appear anywhere in
    `text`? Used both to grade a correct in-scope answer (should be
    present) and, just as importantly, to detect a leak in an
    out-of-scope answer (should NOT be present, under any circumstance -
    not even a coincidental-looking hallucination that happens to match)."""
    for n in _extract_numbers(text):
        if expected == 0:
            if abs(n) < 1:
                return True
        elif abs(n - expected) / abs(expected) < tol:
            return True
    return False


def build_employee_profiles(df):
    """id -> {"name", "role", "own_brand", "own_total"} for every user
    with a role set and at least one file in their own
    data/employees/<name>/ folder. own_brand is whichever brand has the
    largest-magnitude total among their own uploaded rows (their most
    prominent one, if they have more than one) - a concrete, checkable
    entity per person, computed straight from their own folder's data,
    not from anything the app's RBAC layer decided."""
    with db._connect() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT id, name, role FROM users WHERE role IS NOT NULL"
        ).fetchall()]

    profiles = {}
    for user in users:
        folder = DATA_DIR / "employees" / employee_folder_name(user)
        if not folder.is_dir():
            continue
        own_files = {p.name for p in folder.glob("*.xlsx")}
        if not own_files:
            continue
        own_rows = df[df["source_file"].isin(own_files)]
        if own_rows.empty:
            continue
        brand_totals = own_rows.groupby("brand")["net_sale"].sum()
        own_brand = brand_totals.abs().idxmax()
        profiles[user["id"]] = {
            "name": user["name"],
            "role": user["role"],
            "own_brand": own_brand,
            "own_total": float(brand_totals[own_brand]),
        }
    return profiles


def build_pairs(profiles, user_rows, quick):
    """(asker_id, target_id) pairs to test. Full mode: every pair,
    including self-pairs - the full N^2 matrix. Quick mode: self-access
    for everyone, plus (if available) one other in-scope pair and one
    out-of-scope pair per asker."""
    ids = list(profiles.keys())
    if not quick:
        return [(a, t) for a in ids for t in ids]

    pairs = []
    for asker_id in ids:
        pairs.append((asker_id, asker_id))
        accessible = get_accessible_user_ids(user_rows[asker_id])
        in_scope_other = next((t for t in ids if t != asker_id and t in accessible), None)
        out_of_scope = next((t for t in ids if t != asker_id and t not in accessible), None)
        if in_scope_other:
            pairs.append((asker_id, in_scope_other))
        if out_of_scope:
            pairs.append((asker_id, out_of_scope))
    return pairs


def main():
    quick = "--quick" in sys.argv

    df = load_all()
    profiles = build_employee_profiles(df)
    if len(profiles) < 2:
        print("Need at least 2 employees with their own uploaded data to test RBAC scoping.")
        print(f"Found: {len(profiles)}")
        return

    with db._connect() as conn:
        user_rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM users").fetchall()}

    pairs = build_pairs(profiles, user_rows, quick)
    print(f"Testing {len(pairs)} (asker, target) pairs across {len(profiles)} employees"
          f"{' (--quick mode)' if quick else ''}.")
    print()

    results = []
    for i, (asker_id, target_id) in enumerate(pairs, 1):
        asker_row = user_rows[asker_id]
        asker_name = profiles[asker_id]["name"]
        target_profile = profiles[target_id]
        accessible = get_accessible_user_ids(asker_row)
        should_see = target_id in accessible

        # Scope the index exactly the way backend/main.py does: only the
        # asker's own accessible files, nothing else.
        accessible_files = get_accessible_filenames(asker_row, DATA_DIR)
        scoped_df = df[df["source_file"].isin(accessible_files)]

        question = f"What are the total net sales for {target_profile['own_brand']}?"

        if scoped_df.empty:
            response = "(no data available - asker has no accessible files)"
        else:
            idx = build_index_from_df(scoped_df, verbose=False)
            response = answer(question, idx)

        leaked_number = number_present(response, target_profile["own_total"])
        passed = leaked_number if should_see else not leaked_number

        status = "PASS" if passed else "FAIL"
        results.append({
            "asker": asker_name,
            "target": target_profile["name"],
            "target_brand": target_profile["own_brand"],
            "should_see": should_see,
            "status": status,
            "question": question,
            "response": response,
            "expected_total": target_profile["own_total"],
        })

        direction = "IN-SCOPE " if should_see else "OUT-OF-SCOPE"
        print(f"[{i}/{len(pairs)}] {status}  {direction}  "
              f"{asker_name} -> {target_profile['name']}'s {target_profile['own_brand']}")
        print(f"  Q: {question}")
        print(f"  A: {response[:200]}")
        print()

    passed_count = sum(1 for r in results if r["status"] == "PASS")
    in_scope = [r for r in results if r["should_see"]]
    out_of_scope = [r for r in results if not r["should_see"]]
    in_scope_pass = sum(1 for r in in_scope if r["status"] == "PASS")
    out_of_scope_pass = sum(1 for r in out_of_scope if r["status"] == "PASS")

    print("=" * 70)
    print(f"OVERALL: {passed_count}/{len(results)} passed ({passed_count / len(results) * 100:.0f}%)")
    print(f"  In-scope answered correctly:      {in_scope_pass}/{len(in_scope)}"
          f"{f' ({in_scope_pass / len(in_scope) * 100:.0f}%)' if in_scope else ''}")
    print(f"  Out-of-scope correctly refused:   {out_of_scope_pass}/{len(out_of_scope)}"
          f"{f' ({out_of_scope_pass / len(out_of_scope) * 100:.0f}%)' if out_of_scope else ''}")
    print()

    failures = [r for r in results if r["status"] == "FAIL"]
    if failures:
        print("FAILURES:")
        for r in failures:
            kind = "should have answered but didn't" if r["should_see"] else "LEAKED out-of-scope data"
            print(f"  [{kind}] {r['asker']} asking about {r['target']}'s {r['target_brand']}")
            print(f"      expected total: {r['expected_total']:,.0f}")
            print(f"      response: {r['response'][:200]}")
        if any(not r["should_see"] and r["status"] == "FAIL" for r in failures):
            print()
            print("  ^ Any leak above (a real figure appearing where it shouldn't) is a")
            print("    security-relevant failure, not just an accuracy miss - investigate first.")
    else:
        print("No failures.")


if __name__ == "__main__":
    main()
