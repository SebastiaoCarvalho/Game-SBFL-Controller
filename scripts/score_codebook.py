#!/usr/bin/env python3
"""Score two raters' timing-failure codebook sheets.

    python scripts/score_codebook.py results/codebook/rating_sheet_A.csv \\
        results/codebook/rating_sheet_B.csv --out results/codebook/adjudication.csv

Each rater fills a copy of the sheet build_codebook_sheet.py writes. This script
  1. checks every answer is one the codebook allows, and that answered questions cite
     evidence (problems are listed, never guessed around);
  2. derives each rater's category from the answers alone, by the codebook's tree:
       Q1 yes + Q1a same-before            -> F1
       Q1 yes + Q1a same-after             -> F2
       Q1 yes + Q1a other-earlier/later    -> F3
       Q1 no  + not_reached yes            -> not timing
       Q1 no  + Q2 yes                     -> not timing
       Q1 no  + Q2 no                      -> phi mismatch
       anything unsure or missing          -> unresolved
  3. reports Cohen's kappa for Q1 (rows both answered), Q1a (rows both said Q1 yes),
     Q2 (rows both said Q1 no) and the category (all rows), plus the confusion table;
  4. writes a merged sheet with both categories and blank adjudication columns.
"""

import argparse
import csv
import sys
from collections import Counter

ALLOWED = {
    "q1": {"yes", "no", "unsure"},
    "q1a": {"same-before", "same-after", "other-earlier", "other-later"},
    "q2": {"yes", "no", "unsure"},
    "not_reached": {"yes", "no", ""},
    "confidence": {"high", "medium", "low"},
}
F_OF_Q1A = {"same-before": "F1", "same-after": "F2", "other-earlier": "F3", "other-later": "F3"}


def norm(v):
    return (v or "").strip().lower()


def category(r):
    q1, q1a, q2 = norm(r["q1"]), norm(r["q1a"]), norm(r["q2"])
    if q1 == "yes":
        return F_OF_Q1A.get(q1a, "unresolved")
    if q1 == "no":
        if norm(r["not_reached"]) == "yes":
            return "not timing"
        return {"yes": "not timing", "no": "phi mismatch"}.get(q2, "unresolved")
    return "unresolved"


def problems(r):
    out = []
    for col, ok in ALLOWED.items():
        v = norm(r[col])
        if v and v not in ok:
            out.append(f"{col}={r[col]!r} not one of {sorted(x for x in ok if x)}")
    q1 = norm(r["q1"])
    if not q1:
        out.append("q1 unanswered")
    if q1 in ("yes", "no") and not r["q1_evidence"].strip():
        out.append("q1 answered without evidence")
    if q1 == "yes" and not norm(r["q1a"]):
        out.append("q1 is yes but q1a is blank")
    if q1 == "yes" and norm(r["q1a"]) and not r["q1a_evidence"].strip():
        out.append("q1a answered without evidence")
    if q1 == "no" and norm(r["not_reached"]) != "yes" and not norm(r["q2"]):
        out.append("q1 is no but q2 is blank")
    if norm(r["q2"]) in ("yes", "no") and not r["q2_evidence"].strip():
        out.append("q2 answered without evidence")
    return out


def kappa(a, b):
    """Cohen's kappa for two equal-length label lists; None when undefined."""
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if pe == 1:
        return 1.0 if po == 1 else None
    return (po - pe) / (1 - pe)


def load(path):
    rows = {r["row"]: r for r in csv.DictReader(open(path, newline=""))}
    if not rows:
        sys.exit(f"{path}: no rows")
    return rows


def fmt(k):
    return "undefined" if k is None else f"{k:.2f}"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sheet_a")
    ap.add_argument("sheet_b")
    ap.add_argument("--out", help="merged sheet for adjudication")
    args = ap.parse_args(argv)
    A, B = load(args.sheet_a), load(args.sheet_b)
    if set(A) != set(B):
        sys.exit(f"the sheets rate different rows: only A {sorted(set(A) - set(B))}, "
                 f"only B {sorted(set(B) - set(A))}")
    rows = sorted(A, key=int)
    name_a = next(iter(A.values()))["rater"] or "A"
    name_b = next(iter(B.values()))["rater"] or "B"

    bad = [(who, r, p) for who, S in ((name_a, A), (name_b, B)) for r in rows
           for p in problems(S[r])]
    for who, r, p in bad:
        print(f"[check] {who} row {r}: {p}")

    cat_a = {r: category(A[r]) for r in rows}
    cat_b = {r: category(B[r]) for r in rows}

    def pairs(col, keep):
        sel = [r for r in rows if keep(r)]
        return sel, [norm(A[r][col]) for r in sel], [norm(B[r][col]) for r in sel]

    print(f"\n{len(rows)} rows rated by {name_a} and {name_b}")
    for label, col, keep in (
            ("Q1", "q1", lambda r: norm(A[r]["q1"]) and norm(B[r]["q1"])),
            ("Q1a", "q1a", lambda r: norm(A[r]["q1"]) == norm(B[r]["q1"]) == "yes"),
            ("Q2", "q2", lambda r: norm(A[r]["q1"]) == norm(B[r]["q1"]) == "no")):
        sel, a, b = pairs(col, keep)
        agree = sum(x == y for x, y in zip(a, b))
        print(f"  {label:9s} n={len(sel):3d}  agreement {agree}/{len(sel)}  kappa {fmt(kappa(a, b))}")
    a, b = [cat_a[r] for r in rows], [cat_b[r] for r in rows]
    agree = sum(x == y for x, y in zip(a, b))
    print(f"  category  n={len(rows):3d}  agreement {agree}/{len(rows)}  kappa {fmt(kappa(a, b))}")

    cats = sorted(set(a) | set(b))
    width = max(len(c) for c in cats) + 2
    print(f"\nconfusion ({name_a} rows x {name_b} columns)")
    print(" " * width + "".join(f"{c:>{width}}" for c in cats))
    for ca in cats:
        print(f"{ca:{width}}" + "".join(
            f"{sum(1 for r in rows if cat_a[r] == ca and cat_b[r] == cb):>{width}}" for cb in cats))

    dis = [r for r in rows if cat_a[r] != cat_b[r]]
    if dis:
        print("\ndisagreements")
        for r in dis:
            diff = [c for c in ("q1", "q1a", "q2", "not_reached") if norm(A[r][c]) != norm(B[r][c])]
            print(f"  row {r}: {name_a}={cat_a[r]}, {name_b}={cat_b[r]}  (differ on {', '.join(diff)})")

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["row", "game", "branch", f"category_{name_a}", f"category_{name_b}", "agreed",
                        "adjudicated_category", "adjudication_used_probe", "adjudication_notes"])
            for r in rows:
                agreed = cat_a[r] == cat_b[r]
                w.writerow([r, A[r]["game"], A[r]["branch"], cat_a[r], cat_b[r],
                            "yes" if agreed else "no", cat_a[r] if agreed else "", "", ""])
        print(f"\nmerged sheet: {args.out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
