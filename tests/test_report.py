import re
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_report  # noqa: E402

from conftest import run_world  # noqa: E402
from pipeline import warehouse  # noqa: E402


def test_report_is_self_contained_and_numbers_come_from_the_tables(tmp_path):
    import data_generator
    src = tmp_path / "src"
    g = data_generator.CartonCapsDataGenerator(seed=3, output_dir=str(src))
    g.generate_all(n_schools=10, n_users=400, n_products=25, n_purchases=3000, n_referrals=200)
    db = tmp_path / "wh.duckdb"
    con = warehouse.connect(db)
    run_world(src, con=con)
    con.close()

    out = build_report.build(db, None, tmp_path / "r" / "index.html")
    html = out.read_text(encoding="utf-8")

    assert not re.search(r'(src|href)="https?://', html)          # no external requests
    assert "<script src" not in html and "@import" not in html
    assert html.count("<section class=\"card\">") >= 9
    c = duckdb.connect(str(db), read_only=True)
    n_purchases = c.execute("select count(*) from gold.fct_purchases").fetchone()[0]
    top = c.execute("select school_name from gold.mart_school_fundraising order by overall_rank limit 1").fetchone()[0]
    assert f"{n_purchases:,}" in html                              # KPI is computed, not typed
    assert top in html                                             # leaderboard leader appears
    assert "NaN" not in html and "None" not in html
