"""Warehouse bootstrap: connection, schemas, control tables and silver/bronze/quarantine DDL."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import duckdb

from .entities import ENTITIES, Entity

SCHEMAS = ("bronze", "silver", "quarantine", "gold", "meta")


def connect(db_path: Optional[Path | str] = None) -> duckdb.DuckDBPyConnection:
    """Open (or create) the warehouse. ``None`` / ':memory:' gives an in-memory database (tests)."""
    if db_path in (None, ":memory:"):
        con = duckdb.connect(":memory:")
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone='UTC'")
    init(con)
    return con


def init(con: duckdb.DuckDBPyConnection) -> None:
    for s in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")

    con.execute("""
        CREATE TABLE IF NOT EXISTS meta.ingest_log (
            batch_id     BIGINT PRIMARY KEY,
            entity       VARCHAR NOT NULL,
            source       VARCHAR NOT NULL,
            file_hash    VARCHAR NOT NULL,
            row_count    BIGINT,
            ingested_at  TIMESTAMP DEFAULT current_timestamp
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS meta.watermarks (
            entity VARCHAR PRIMARY KEY,
            last_batch_id BIGINT NOT NULL
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS meta.runs (
            run_id      VARCHAR PRIMARY KEY,
            started_at  TIMESTAMP,
            finished_at TIMESTAMP,
            status      VARCHAR,
            error       VARCHAR
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS meta.run_metrics (
            run_id VARCHAR, stage VARCHAR, entity VARCHAR, metric VARCHAR, value DOUBLE,
            recorded_at TIMESTAMP DEFAULT current_timestamp
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS meta.dq_results (
            run_id VARCHAR, entity VARCHAR, check_name VARCHAR, severity VARCHAR,
            failed_rows BIGINT, total_rows BIGINT, status VARCHAR,
            recorded_at TIMESTAMP DEFAULT current_timestamp
        )""")
    con.execute("CREATE TABLE IF NOT EXISTS meta.pending_gold_dates (d DATE PRIMARY KEY)")

    for e in ENTITIES:
        _create_entity_tables(con, e)


def _create_entity_tables(con: duckdb.DuckDBPyConnection, e: Entity) -> None:
    raw_cols = ", ".join(f"{c} VARCHAR" for c in e.raw_cols)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS bronze.{e.name} (
            {raw_cols},
            _batch_id BIGINT NOT NULL, _row_num BIGINT NOT NULL,
            _source VARCHAR, _ingested_at TIMESTAMP DEFAULT current_timestamp
        )""")
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS quarantine.{e.name} (
            {raw_cols},
            _batch_id BIGINT, _row_num BIGINT, _source VARCHAR,
            _dq_reasons VARCHAR, _retryable BOOLEAN,
            _first_quarantined_at TIMESTAMP, _last_checked_at TIMESTAMP, _run_id VARCHAR
        )""")
    silver_cols = ", ".join(f"{n} {t}" for n, t in e.silver_cols)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS silver.{e.name} (
            {silver_cols},
            _batch_id BIGINT, _loaded_at TIMESTAMP,
            PRIMARY KEY ({e.key})
        )""")


def reset_derived_layers(con: duckdb.DuckDBPyConnection) -> None:
    """Throw away silver/quarantine/gold + watermarks, keep bronze (the immutable raw log).
    Used by --rebuild-from-bronze: proves the whole warehouse is reproducible from raw."""
    for e in ENTITIES:
        con.execute(f"DELETE FROM silver.{e.name}")
        con.execute(f"DELETE FROM quarantine.{e.name}")
    con.execute("DELETE FROM meta.watermarks")
    con.execute("DELETE FROM meta.pending_gold_dates")
    for (t,) in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='gold'").fetchall():
        con.execute(f"DROP TABLE IF EXISTS gold.{t}")
