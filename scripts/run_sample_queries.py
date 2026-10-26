"""Run every query in queries/sample_queries.sql against the warehouse and print the results.

    python scripts/run_sample_queries.py [--db warehouse/carton_caps.duckdb] [--markdown]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent


def load_queries(path: Path):
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"^-- (Q\d+): (.*)$", text, flags=re.M)
    # parts = [preamble, id, title, body, id, title, body, ...]
    for i in range(1, len(parts), 3):
        yield parts[i], parts[i + 1].strip(), parts[i + 2].strip().rstrip(";")


def fmt(v):
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}"
    return "" if v is None else str(v)


def render(cols, rows, markdown: bool) -> str:
    cells = [[fmt(v) for v in r] for r in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) if cells else len(c) for i, c in enumerate(cols)]
    if markdown:
        out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
        out += ["| " + " | ".join(r) + " |" for r in cells]
        return "\n".join(out)
    out = ["  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths))]
    out.append("  " + "  ".join("-" * w for w in widths))
    out += ["  " + "  ".join(v.ljust(w) for v, w in zip(r, widths)) for r in cells]
    return "\n".join(out)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windows consoles default to cp1252
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "warehouse" / "carton_caps.duckdb"))
    ap.add_argument("--queries", default=str(ROOT / "queries" / "sample_queries.sql"))
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--only", help="comma separated ids, e.g. Q1,Q4")
    a = ap.parse_args()

    if not Path(a.db).exists():
        print(f"{a.db} not found - run `python -m pipeline.run` first", file=sys.stderr)
        return 1
    con = duckdb.connect(a.db, read_only=True)
    only = set(a.only.split(",")) if a.only else None
    for qid, title, sql in load_queries(Path(a.queries)):
        if only and qid not in only:
            continue
        res = con.execute(sql)
        cols = [d[0] for d in res.description]
        rows = res.fetchall()
        print(f"\n### {qid}: {title}\n" if a.markdown else f"\n{qid}: {title}")
        if a.markdown:
            print(render(cols, rows, True))
        else:
            print(render(cols, rows, False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
