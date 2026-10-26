"""
Silver: typed, validated, de-duplicated, conformed.

For each entity, in dependency order, inside one transaction:

  1. stage   = bronze rows from batches newer than the entity's watermark
               + quarantined rows flagged retryable (late-arriving parents)
  2. type    = cast raw text -> typed columns (bad casts become NULL, then get flagged)
  3. flag    = evaluate every DQ rule (entities.py) as a 0/1 column
  4. split   = rows with any *error* flag -> quarantine (with reasons); the rest continue
  5. dedupe  = per business key keep the latest (batch, row) among rows that PASSED, so a bad
               update can never overwrite a good record
  6. merge   = INSERT OR REPLACE into silver (idempotent upsert), remember which purchase dates moved
  7. circuit breaker = if the reject rate of *new* rows exceeds the threshold, ROLLBACK and raise
  8. advance watermark, write dq_results + metrics

Why watermark on batch id and not on a source timestamp: the sources don't carry an updated_at, and
arrival order is the only ordering we can trust. Batch id is assigned by ingestion, monotonic.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List

import duckdb

from .config import Settings
from .entities import ENTITIES, Entity, Rule

log = logging.getLogger("pipeline.silver")


class DataQualityError(RuntimeError):
    """Raised when a batch breaches the reject-rate circuit breaker."""


DQ_INSERT = ("INSERT INTO meta.dq_results (run_id, entity, check_name, severity, failed_rows, total_rows, status) "
             "VALUES (?,?,?,?,?,?,?)")


def _typed_select(e: Entity) -> str:
    typed = [f"{c.typed_expr().format(raw='s.' + c.name)} AS {c.name}" for c in e.cols]
    raw = [f"s.{c} AS r_{c}" for c in e.raw_cols]
    return ", ".join(typed + raw)


def _flag_select(e: Entity) -> str:
    parts = []
    for c in e.cols:
        if c.required:
            parts.append(
                f"CASE WHEN typed.{c.name} IS NULL AND (typed.r_{c.name} IS NULL OR trim(typed.r_{c.name}) = '') "
                f"THEN 1 ELSE 0 END AS f_null_{c.name}")
        if c.dtype != "VARCHAR":
            parts.append(
                f"CASE WHEN typed.{c.name} IS NULL AND typed.r_{c.name} IS NOT NULL AND trim(typed.r_{c.name}) <> '' "
                f"THEN 1 ELSE 0 END AS f_invalid_{c.name}")
    for r in e.rules:
        parts.append(f"CASE WHEN COALESCE(({r.predicate}), false) THEN 1 ELSE 0 END AS f_{r.name}")
    return ", ".join(parts)


def _rule_catalog(e: Entity) -> List[Rule]:
    """All rules incl. the auto-generated null / cast checks."""
    rules: List[Rule] = []
    for c in e.cols:
        if c.required:
            rules.append(Rule(f"null_{c.name}", "", "error"))
        if c.dtype != "VARCHAR":
            rules.append(Rule(f"invalid_{c.name}", "", "error"))
    rules.extend(e.rules)
    return rules


def process_entity(con: duckdb.DuckDBPyConnection, e: Entity, settings: Settings, run_id: str) -> Dict[str, float]:
    t0 = time.time()
    n = e.name
    raw_cols = ", ".join(e.raw_cols)
    wm = con.execute("SELECT coalesce(max(last_batch_id), 0) FROM meta.watermarks WHERE entity=?", [n]).fetchone()[0]
    max_batch = con.execute(f"SELECT coalesce(max(_batch_id), 0) FROM bronze.{n}").fetchone()[0]

    new_rows = con.execute(f"SELECT count(*) FROM bronze.{n} WHERE _batch_id > {wm}").fetchone()[0]
    retry_rows = con.execute(f"SELECT count(*) FROM quarantine.{n} WHERE _retryable").fetchone()[0]
    if new_rows == 0 and retry_rows == 0:
        return {"new_rows": 0, "retried": 0, "quarantined": 0, "merged": 0}

    catalog = _rule_catalog(e)
    errors = [r for r in catalog if r.severity == "error"]
    hard_errors = [r for r in errors if not r.retryable]

    def any_flag(rs):
        return " OR ".join(f"f_{r.name} = 1" for r in rs) or "false"

    reasons = ", ".join(f"CASE WHEN f_{r.name} = 1 THEN '{r.name}' END" for r in errors)

    dq_rows: list = []
    con.execute("BEGIN")
    try:
        # 1-3. stage, type, flag ------------------------------------------------------------------
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _scored AS
            WITH stage AS (
                SELECT {raw_cols}, _batch_id, _row_num, _source, false AS _is_retry,
                       CAST(NULL AS TIMESTAMP) AS _first_q
                FROM bronze.{n} WHERE _batch_id > {wm}
                UNION ALL
                SELECT {raw_cols}, _batch_id, _row_num, _source, true, _first_quarantined_at
                FROM quarantine.{n} WHERE _retryable
            ),
            typed AS (
                SELECT {_typed_select(e)}, s._batch_id, s._row_num, s._source, s._is_retry, s._first_q
                FROM stage s
            ),
            flagged AS (
                SELECT typed.*, {_flag_select(e)}
                FROM typed {e.joins}
            )
            SELECT *,
                   concat_ws(',', {reasons}) AS _err_reasons,
                   ({any_flag(errors)}) AS _is_error,
                   NOT ({any_flag(hard_errors)}) AS _retryable
            FROM flagged
        """)

        # 4. release the old retry set; whatever still fails goes back below ------------------------
        con.execute(f"DELETE FROM quarantine.{n} WHERE _retryable")
        con.execute(f"""
            INSERT INTO quarantine.{n}
            SELECT {', '.join('r_' + c for c in e.raw_cols)}, _batch_id, _row_num, _source,
                   _err_reasons, _retryable,
                   coalesce(_first_q, current_timestamp), current_timestamp, '{run_id}'
            FROM _scored WHERE _is_error""")

        # 5. de-duplicate the survivors, latest batch/row wins -------------------------------------
        silver_typed = [c.name for c in e.cols]
        derived_sql = "".join(f", {d.expr} AS {d.name}" for d in e.derived)
        silver_all = silver_typed + [d.name for d in e.derived]
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _final AS
            SELECT {', '.join(silver_all)}, _batch_id FROM (
                SELECT {', '.join(silver_typed)}{derived_sql}, _batch_id,
                       row_number() OVER (PARTITION BY {e.key} ORDER BY _batch_id DESC, _row_num DESC) AS rn
                FROM _scored WHERE NOT _is_error
            ) WHERE rn = 1""")

        # 6. remember which event dates are about to change (old and new) so gold rebuilds only those -------
        if e.date_col:
            con.execute(f"""
                INSERT OR IGNORE INTO meta.pending_gold_dates
                SELECT {e.date_col} FROM silver.{n} WHERE {e.key} IN (SELECT {e.key} FROM _final)
                UNION SELECT {e.date_col} FROM _final""")
        con.execute(f"""
            INSERT OR REPLACE INTO silver.{n} ({', '.join(silver_all)}, _batch_id, _loaded_at)
            SELECT {', '.join(silver_all)}, _batch_id, current_timestamp FROM _final""")

        # 7. metrics + DQ results -------------------------------------------------------------------
        sums = ", ".join(f"coalesce(sum(CASE WHEN NOT _is_retry THEN f_{r.name} END), 0) AS {r.name}" for r in catalog)
        counts = con.execute(f"""
            SELECT count(*) FILTER (WHERE NOT _is_retry)                                   AS new_rows,
                   count(*) FILTER (WHERE _is_retry)                                       AS retried,
                   count(*) FILTER (WHERE _is_retry AND NOT _is_error)                     AS recovered,
                   count(*) FILTER (WHERE NOT _is_retry AND _is_error)                     AS quarantined,
                   count(*) FILTER (WHERE NOT _is_retry AND NOT _is_error)                 AS passed,
                   count(DISTINCT {e.key}) FILTER (WHERE NOT _is_retry AND NOT _is_error)  AS passed_keys,
                   {sums}
            FROM _scored""").fetchone()
        cols = [d[0] for d in con.description]
        row = dict(zip(cols, counts))
        merged = con.execute("SELECT count(*) FROM _final").fetchone()[0]
        duplicates = row["passed"] - row["passed_keys"]
        reject_rate = (row["quarantined"] / row["new_rows"]) if row["new_rows"] else 0.0

        dq_rows = [
            (run_id, n, r.name, r.severity, int(row[r.name]), int(row["new_rows"]),
             "pass" if row[r.name] == 0 else ("fail" if r.severity == "error" else "warn"))
            for r in catalog
        ]
        if row["new_rows"] >= settings.breaker_min_rows and reject_rate > settings.max_reject_rate:
            raise DataQualityError(
                f"{n}: {row['quarantined']}/{row['new_rows']} new rows rejected "
                f"({reject_rate:.1%} > {settings.max_reject_rate:.0%} threshold). Top reasons: "
                + ", ".join(f"{r.name}={row[r.name]}" for r in sorted(errors, key=lambda r: -row[r.name])[:4] if row[r.name]))

        # 8. commit state -----------------------------------------------------------------------------
        con.execute("DELETE FROM meta.watermarks WHERE entity=?", [n])
        con.execute("INSERT INTO meta.watermarks VALUES (?, ?)", [n, max_batch])

        con.executemany(DQ_INSERT, dq_rows)
        metrics = {"new_rows": row["new_rows"], "retried": row["retried"], "recovered": row["recovered"],
                   "quarantined": row["quarantined"], "duplicates_collapsed": duplicates, "merged": merged,
                   "reject_rate": reject_rate, "seconds": round(time.time() - t0, 3)}
        con.executemany("INSERT INTO meta.run_metrics (run_id, stage, entity, metric, value) VALUES (?,?,?,?,?)",
                        [(run_id, "silver", n, k, float(v)) for k, v in metrics.items()])
        con.execute("COMMIT")
    except Exception as exc:
        con.execute("ROLLBACK")
        if isinstance(exc, DataQualityError) and dq_rows:
            # silver/quarantine/watermark changes are rolled back, but the evidence must survive
            con.executemany(DQ_INSERT, dq_rows)
        raise

    log.info("silver.%s new=%s retried=%s recovered=%s quarantined=%s dupes=%s merged=%s",
             n, metrics["new_rows"], metrics["retried"], metrics["recovered"],
             metrics["quarantined"], metrics["duplicates_collapsed"], metrics["merged"])
    return metrics


def process_all(con: duckdb.DuckDBPyConnection, settings: Settings, run_id: str) -> Dict[str, dict]:
    return {e.name: process_entity(con, e, settings, run_id) for e in ENTITIES}
