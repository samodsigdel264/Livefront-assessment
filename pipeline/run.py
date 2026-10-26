"""
Pipeline entrypoint.

    python -m pipeline.run                         # ingest data/*.csv -> bronze -> silver -> gold -> parquet
    python -m pipeline.run --sqlite data/carton_caps_generated.db
    python -m pipeline.run --sqlite Carton_Caps_Data.sqlite   # the challenge's sample DB (schema auto-detected)
    python -m pipeline.run --rebuild-from-bronze   # re-derive silver+gold from the raw log
    python -m pipeline.run --max-reject-rate 0.02  # tighter circuit breaker

Exit codes: 0 ok, 2 data-quality circuit breaker tripped / reconciliation failed, 1 anything else.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import gold, ingest, quality, silver, warehouse
from .config import ROOT, Settings

log = logging.getLogger("pipeline")


def run(settings: Settings, sources: List[ingest.Source], rebuild_from_bronze: bool = False,
        export: bool = True, con=None) -> dict:
    own_con = con is None
    con = con or warehouse.connect(settings.db_path)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    con.execute("INSERT INTO meta.runs (run_id, started_at, status) VALUES (?, current_timestamp, 'running')", [run_id])
    summary = {"run_id": run_id}
    try:
        if rebuild_from_bronze:
            log.info("rebuilding silver/gold from bronze")
            warehouse.reset_derived_layers(con)

        summary["ingest"] = ingest.ingest_all(con, sources)
        summary["silver"] = silver.process_all(con, settings, run_id)
        changed = any(m.get("merged", 0) > 0 or m.get("recovered", 0) > 0 for m in summary["silver"].values())
        gold_exists = con.execute("SELECT count(*) FROM information_schema.tables "
                                  "WHERE table_schema='gold' AND table_name='dim_user'").fetchone()[0] > 0
        if changed or not gold_exists:
            summary["gold"] = gold.build_gold(con, settings, run_id)
        else:
            log.info("silver unchanged and gold present: skipping gold rebuild")
            summary["gold"] = {}
        failures = quality.run_reconciliation(con, run_id) if summary["gold"] else []
        summary["reconciliation_failures"] = failures
        if failures:
            raise silver.DataQualityError(f"gold reconciliation failed: {failures}")
        if export and summary["gold"]:
            summary["exported"] = gold.export_gold(con, settings.export_dir)
        con.execute("UPDATE meta.runs SET finished_at=current_timestamp, status='success' WHERE run_id=?", [run_id])
    except Exception as exc:
        con.execute("UPDATE meta.runs SET finished_at=current_timestamp, status='failed', error=? WHERE run_id=?",
                    [str(exc)[:1000], run_id])
        raise
    finally:
        if own_con:
            con.close()
    return summary


def _print_summary(s: dict) -> None:
    print(f"\nrun {s['run_id']}")
    print("-" * 78)
    print(f"{'entity':<11}{'ingested':>9}{'new':>8}{'retried':>8}{'recov.':>7}{'quarant.':>9}{'dupes':>7}{'merged':>8}")
    for e, m in s["silver"].items():
        print(f"{e:<11}{s['ingest'][e]['rows']:>9}{m.get('new_rows', 0):>8.0f}{m.get('retried', 0):>8.0f}"
              f"{m.get('recovered', 0):>7.0f}{m.get('quarantined', 0):>9.0f}{m.get('duplicates_collapsed', 0):>7.0f}"
              f"{m.get('merged', 0):>8.0f}")
    if not s.get("gold"):
        print("\ngold: up to date (no new data), nothing rebuilt")
    if s.get("gold"):
        print(f"\ngold models built: {len(s['gold'])}   reconciliation failures: {len(s['reconciliation_failures'])}")
        if s.get("exported"):
            print(f"parquet exported: {len(s['exported'])} tables")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Carton Caps analytics pipeline")
    ap.add_argument("--source-dir", default=str(ROOT / "data"), help="directory with <entity>.csv files")
    ap.add_argument("--sqlite", help="read from a SQLite database instead of CSVs (our own output or the challenge's sample DB, auto-detected)")
    ap.add_argument("--db", default=str(Settings().db_path))
    ap.add_argument("--export-dir", default=str(Settings().export_dir))
    ap.add_argument("--max-reject-rate", type=float, default=Settings().max_reject_rate)
    ap.add_argument("--point-value-usd", type=float, default=Settings().point_value_usd)
    ap.add_argument("--rebuild-from-bronze", action="store_true")
    ap.add_argument("--no-export", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    settings = Settings(db_path=Path(a.db), export_dir=Path(a.export_dir),
                        max_reject_rate=a.max_reject_rate, point_value_usd=a.point_value_usd)
    try:
        source = ingest.open_sqlite(a.sqlite) if a.sqlite else ingest.CsvSource(a.source_dir)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    if not a.sqlite and not Path(a.source_dir).is_dir():
        log.error("source directory not found: %s", Path(a.source_dir).resolve())
        return 1
    try:
        summary = run(settings, [source], rebuild_from_bronze=a.rebuild_from_bronze, export=not a.no_export)
    except silver.DataQualityError as exc:
        log.error("DATA QUALITY GATE: %s", exc)
        return 2
    except ingest.SchemaContractError as exc:
        log.error("SCHEMA CONTRACT: %s", exc)
        return 2
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
