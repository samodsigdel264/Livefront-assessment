"""
Gold: dimensional model + analytical marts.

Models are plain SQL files in pipeline/sql/gold, run in filename order (a poor man's dbt DAG; in
production these map 1:1 onto dbt models, see docs/ARCHITECTURE.md).

Two kinds:
  * full-refresh models  - CREATE OR REPLACE TABLE ... (dimensions and marts; cheap aggregates)
  * partitioned-incremental models - the file is a bare SELECT with a header comment
        -- incremental: table=gold.fct_purchases partition=purchase_date
    On a normal run only the partitions listed in meta.pending_gold_dates (filled by silver for every
    purchase date whose rows were inserted *or replaced*) are deleted and re-inserted. First run,
    or after --rebuild-from-bronze, builds all partitions.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import duckdb

from .config import SQL_DIR, Settings

log = logging.getLogger("pipeline.gold")

_INCR_HEADER = re.compile(r"--\s*incremental:\s*table=(\S+)\s+partition=(\S+)")
_PART_FILTER = re.compile(r"\{\{PARTITION_FILTER:([^}]+)\}\}")


@dataclass(frozen=True)
class Model:
    path: Path
    # templating overrides, e.g. the cohort model is instantiated for week and month grains
    params: tuple = ()
    label: Optional[str] = None


def discover_models() -> List[Model]:
    models: List[Model] = []
    for p in sorted((SQL_DIR / "gold").glob("*.sql")):
        if p.name.startswith("21_"):
            models.append(Model(p, (("GRAIN", "week"), ("TABLE", "mart_cohort_retention_weekly")), "cohort_weekly"))
            models.append(Model(p, (("GRAIN", "month"), ("TABLE", "mart_cohort_retention_monthly")), "cohort_monthly"))
        else:
            models.append(Model(p))
    return models


def _render(sql: str, params: Dict[str, str]) -> str:
    for k, v in params.items():
        sql = sql.replace("{{" + k + "}}", v)
    return sql


def _table_exists(con, qualified: str) -> bool:
    schema, name = qualified.split(".")
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema=? AND table_name=?",
                       [schema, name]).fetchone()[0] > 0


def _run_incremental(con, sql: str, table: str, part_col_name: str, params: Dict[str, str]) -> Dict[str, float]:
    full = not _table_exists(con, table)
    pending = con.execute("SELECT count(*) FROM meta.pending_gold_dates").fetchone()[0]

    def build(filter_sql_for):
        return _PART_FILTER.sub(lambda m: filter_sql_for(m.group(1)), _render(sql, params))

    if full:
        con.execute(f"CREATE TABLE {table} AS {build(lambda col: '1=1')}")
        parts = con.execute(f"SELECT count(DISTINCT {part_col_name}) FROM {table}").fetchone()[0]
    elif pending == 0:
        return {"partitions_rebuilt": 0, "rows": 0}
    else:
        con.execute(f"DELETE FROM {table} WHERE {part_col_name} IN (SELECT d FROM meta.pending_gold_dates)")
        con.execute(f"INSERT INTO {table} {build(lambda col: f'{col} IN (SELECT d FROM meta.pending_gold_dates)')}")
        parts = pending
    rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return {"partitions_rebuilt": parts, "rows": rows}


def build_gold(con: duckdb.DuckDBPyConnection, settings: Settings, run_id: str,
               models: Optional[List[Model]] = None) -> Dict[str, dict]:
    results: Dict[str, dict] = {}
    if con.execute("SELECT count(*) FROM silver.purchases").fetchone()[0] == 0 and \
       con.execute("SELECT count(*) FROM silver.users").fetchone()[0] == 0:
        log.warning("silver is empty; skipping gold")
        return results

    base_params = settings.sql_params()
    con.execute("BEGIN")
    try:
        for m in (models or discover_models()):
            t0 = time.time()
            params = {**base_params, **dict(m.params)}
            sql = m.path.read_text(encoding="utf-8")
            name = m.label or m.path.stem
            hdr = _INCR_HEADER.search(sql)
            if hdr:
                res = _run_incremental(con, sql, hdr.group(1), hdr.group(2), params)
            else:
                con.execute(_render(sql, params))
                res = {}
            res["seconds"] = round(time.time() - t0, 3)
            results[name] = res
            log.info("gold %-38s %s", name, res)
            for k, v in res.items():
                con.execute("INSERT INTO meta.run_metrics (run_id, stage, entity, metric, value) VALUES (?,?,?,?,?)",
                            [run_id, "gold", name, k, float(v)])
        con.execute("DELETE FROM meta.pending_gold_dates")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")   # pending dates survive, the next run retries the same partitions
        raise
    return results


def export_gold(con: duckdb.DuckDBPyConnection, export_dir: Path) -> List[str]:
    """Write every gold table to Parquet: the portable, engine-agnostic serving format."""
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    names = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='gold' ORDER BY 1").fetchall()]
    for t in names:
        out = (export_dir / f"{t}.parquet").as_posix()
        con.execute(f"COPY (SELECT * FROM gold.{t}) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    return names
