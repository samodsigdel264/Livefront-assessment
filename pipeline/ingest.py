"""
Bronze ingestion.

Principles
  * Bronze is an append-only, all-VARCHAR copy of whatever arrived, stamped with batch id, row number,
    source and load time. Nothing is cast, rejected or fixed here, so a bad value can never block
    landing the data, and silver can always be rebuilt from bronze.
  * Idempotent: a source (file / table snapshot) is identified by its content hash. Re-running the
    pipeline on the same inputs is a no-op; a *new* file for an entity is a new batch.
  * Schema contract: required columns must be present (hard fail); unknown extra columns are
    reported as drift and ignored.

Sources are pluggable: ``CsvSource`` (a directory of ``<entity>.csv`` and ``<entity>__<suffix>.csv``
increments), ``SqliteSource`` (a SQLite database whose table names match the entities) and
``SampleSchemaSource`` (an adapter for the SQLite file shipped with the challenge, whose tables are
Schools / Users / Products / Purchase_History / Conversation_History with different column names).
``open_sqlite`` picks between the last two by looking at the tables.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Protocol

import duckdb

from .entities import ENTITIES, Entity

log = logging.getLogger("pipeline.ingest")


class SchemaContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceUnit:
    entity: str
    label: str          # human readable origin, stored as _source
    file_hash: str


class Source(Protocol):
    def units(self, entity: Entity) -> List[SourceUnit]: ...
    def read(self, con: duckdb.DuckDBPyConnection, unit: SourceUnit) -> str:
        """Register the unit as a relation / view and return its name (all columns VARCHAR)."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class CsvSource:
    def __init__(self, directory: Path | str):
        self.dir = Path(directory)

    def units(self, entity: Entity) -> List[SourceUnit]:
        files = sorted(self.dir.glob(f"{entity.name}.csv")) + sorted(self.dir.glob(f"{entity.name}__*.csv"))
        return [SourceUnit(entity.name, str(p), _sha256_file(p)) for p in files]

    def read(self, con, unit: SourceUnit) -> str:
        view = f"_src_{unit.entity}"
        path = unit.label.replace("'", "''")
        con.execute(f"CREATE OR REPLACE TEMP VIEW {view} AS "
                    f"SELECT * FROM read_csv('{path}', header=true, all_varchar=true)")
        return view


class SqliteSource:
    """Reads tables named after the entities from a SQLite file (e.g. data/carton_caps_generated.db)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def units(self, entity: Entity) -> List[SourceUnit]:
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (entity.name,)).fetchone()
        if not exists:
            return []
        return [SourceUnit(entity.name, f"{self.path}#{entity.name}", f"{_sha256_file(self.path)}:{entity.name}")]

    def read(self, con, unit: SourceUnit) -> str:
        view = f"_src_{unit.entity}"
        with sqlite3.connect(self.path) as c:
            cur = c.execute(f'SELECT * FROM "{unit.entity}"')
            cols = [d[0] for d in cur.description]
            rows = [tuple(None if v is None else str(v) for v in r) for r in cur.fetchall()]
        con.execute(f"CREATE OR REPLACE TEMP TABLE {view} ({', '.join(f'{c} VARCHAR' for c in cols)})")
        if rows:
            con.executemany(f"INSERT INTO {view} VALUES ({', '.join('?' for _ in cols)})", rows)
        return view


# ------------------------------------------------------------------------------------------------
# Adapter for the challenge's sample database
# ------------------------------------------------------------------------------------------------
_ADDR = re.compile(r"^.+,\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$")
_CATEGORY_KEYWORDS = (   # first match wins; the sample catalogue has no category column
    ("Beverages", ("juice", "drink", "tea", "coffee", "soda", "water", "lemonade")),
    ("Breakfast", ("cereal", "oatmeal", "granola", "pancake", "waffle", "frosted")),
    ("Pantry", ("pasta", "macaroni", "rice", "vegetable", "soup", "sauce", "beans", "noodle")),
    ("Dairy", ("milk", "yogurt", "cheese")),            # after Pantry: "Macaroni & Cheese" is a pasta box
    ("Snacks", ("snack", "cracker", "chips", "trail mix", "popcorn", "cookie", "bar")),
)
DEFAULT_POINTS_PER_DOLLAR = 2   # sample catalogue has no earn rate; 2 sits between the generator's 1/2/3/5


def _split_name(full: Optional[str]):
    parts = (full or "").split()
    return (parts[0], " ".join(parts[1:]) or None) if parts else (None, None)


def _infer_category(name: str, description: str) -> str:
    text = f"{name} {description}".lower()
    name_l = name.lower()
    for cat, words in _CATEGORY_KEYWORDS:           # the product NAME decides first, the blurb only as a fallback
        if any(w in name_l for w in words):
            return cat
    for cat, words in _CATEGORY_KEYWORDS:
        if any(w in text for w in words):
            return cat
    return "Other"


def _schools(rows):
    for id_, name, address, created in rows:
        m = _ADDR.match(address or "")
        yield (id_, name, address, m["city"] if m else None, m["state"] if m else None, m["zip"] if m else None, created)


def _users(rows):
    for id_, school_id, name, email, created in rows:
        first, last = _split_name(name)
        yield (id_, first, last, email, school_id, created, None)


def _products(rows):
    for id_, name, description, price, created in rows:
        yield (id_, name, _infer_category(name, description or ""), price, DEFAULT_POINTS_PER_DOLLAR, created)


class SampleSchemaSource(SqliteSource):
    """Maps the challenge's sample database onto the pipeline's contract. Every mapping is a view of
    the source: nothing is invented except where a canonical column has no counterpart, and each of
    those is listed here and in docs/DATA_ASSUMPTIONS.md.

      Schools.id/name/address/created_at   -> schools; city/state/zip parsed from address when it ends '..., City, ST 12345'
      Users.name                           -> first_name + last_name (split on first space); user_type absent -> 'unknown'
      Products                             -> category inferred from the name (keywords), points_per_dollar = 2
      Purchase_History                     -> purchases; price_paid = quantity x list price (no price column in the
                                              source), points_earned left NULL (derived in gold from points_per_dollar)
      Conversation_History                 -> chat_messages (timestamp -> sent_at)
      referrals                            -> no source table; the entity is simply empty
    """

    # entity -> (SQL over the sample tables, column order = the entity's raw columns, row mapper)
    MAPPINGS = {
        "schools": ("SELECT id, name, address, created_at FROM Schools ORDER BY id",
                    ["school_id", "name", "address", "city", "state", "zip_code", "created_at"], _schools),
        "users": ("SELECT id, school_id, name, email, created_at FROM Users ORDER BY id",
                  ["user_id", "first_name", "last_name", "email", "school_id", "created_at", "user_type"], _users),
        "products": ("SELECT id, name, description, price, created_at FROM Products ORDER BY id",
                     ["product_id", "name", "category", "price", "points_per_dollar", "created_at"], _products),
        "purchases": ("SELECT h.id, h.user_id, h.product_id, h.quantity, round(h.quantity * p.price, 2), NULL, h.purchased_at "
                      "FROM Purchase_History h LEFT JOIN Products p ON p.id = h.product_id ORDER BY h.id",
                      ["purchase_id", "user_id", "product_id", "quantity", "price_paid", "points_earned", "purchased_at"],
                      None),
        "chat_messages": ("SELECT id, user_id, message, sender, timestamp FROM Conversation_History ORDER BY id",
                          ["message_id", "user_id", "message", "sender", "sent_at"], None),
    }
    SOURCE_TABLE = {"schools": "Schools", "users": "Users", "products": "Products",
                    "purchases": "Purchase_History", "chat_messages": "Conversation_History"}

    def units(self, entity: Entity) -> List[SourceUnit]:
        table = self.SOURCE_TABLE.get(entity.name)
        if table is None or not self.path.exists():
            return []
        with sqlite3.connect(self.path) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND lower(name)=lower(?)", (table,)).fetchone():
                return []
        return [SourceUnit(entity.name, f"{self.path}#{table}", f"{_sha256_file(self.path)}:{table}")]

    def read(self, con, unit: SourceUnit) -> str:
        sql, cols, mapper = self.MAPPINGS[unit.entity]
        with sqlite3.connect(self.path) as c:
            rows = c.execute(sql).fetchall()
        rows = list(mapper(rows)) if mapper else rows
        view = f"_src_{unit.entity}"
        con.execute(f"CREATE OR REPLACE TEMP TABLE {view} ({', '.join(f'{c} VARCHAR' for c in cols)})")
        if rows:
            con.executemany(f"INSERT INTO {view} VALUES ({', '.join('?' for _ in cols)})",
                            [tuple(None if v is None else str(v) for v in r) for r in rows])
        return view


def open_sqlite(path: Path | str) -> Source:
    """Pick the right SQLite reader: canonical table names (our own generator output) or the challenge's sample schema."""
    path = Path(path)
    if not path.is_file():      # sqlite3.connect() would silently create an empty database here
        raise FileNotFoundError(f"SQLite file not found: {path.resolve()}")
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as c:
        names = {r[0].lower() for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        user_cols = {r[1].lower() for r in c.execute("PRAGMA table_info(Users)")}
    if names & {"purchase_history", "conversation_history"} or ("users" in names and "schools" in names and "id" in user_cols):
        log.info("%s: detected the challenge's sample schema, using the adapter", path)
        return SampleSchemaSource(path)
    return SqliteSource(path)


def _check_contract(con, view: str, entity: Entity, label: str) -> List[str]:
    present = {r[0].lower() for r in con.execute(f"DESCRIBE {view}").fetchall()}
    required = {c.name for c in entity.cols}
    missing = sorted(required - present)
    if missing:
        raise SchemaContractError(f"{label}: missing required column(s) {missing}")
    extra = sorted(present - set(entity.raw_cols))
    if extra:
        log.warning("schema drift in %s: ignoring unknown column(s) %s", label, extra)
    return sorted(present)


def ingest_entity(con: duckdb.DuckDBPyConnection, source: Source, entity: Entity) -> dict:
    """Load every not-yet-seen unit of ``entity`` from ``source``. Returns {'batches': n, 'rows': n}."""
    out = {"batches": 0, "rows": 0, "skipped": 0}
    for unit in source.units(entity):
        seen = con.execute(
            "SELECT 1 FROM meta.ingest_log WHERE entity=? AND file_hash=?", [entity.name, unit.file_hash]).fetchone()
        if seen:
            out["skipped"] += 1
            log.info("skip %s (already ingested, hash %s...)", unit.label, unit.file_hash[:8])
            continue

        view = source.read(con, unit)
        present = set(_check_contract(con, view, entity, unit.label))
        batch_id = con.execute("SELECT coalesce(max(batch_id), 0) + 1 FROM meta.ingest_log").fetchone()[0]

        select_cols = ", ".join(
            (f'CAST("{c}" AS VARCHAR) AS {c}' if c in present else f"CAST(NULL AS VARCHAR) AS {c}")
            for c in entity.raw_cols)
        label = unit.label.replace("'", "''")
        con.execute("BEGIN")
        try:
            con.execute(f"""
                INSERT INTO bronze.{entity.name}
                SELECT {select_cols}, {batch_id}, row_number() OVER (), '{label}', current_timestamp
                FROM {view}""")
            n = con.execute(f"SELECT count(*) FROM bronze.{entity.name} WHERE _batch_id = {batch_id}").fetchone()[0]
            con.execute("INSERT INTO meta.ingest_log (batch_id, entity, source, file_hash, row_count) VALUES (?,?,?,?,?)",
                        [batch_id, entity.name, unit.label, unit.file_hash, n])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        out["batches"] += 1
        out["rows"] += n
        log.info("ingested %s -> bronze.%s batch=%s rows=%s", unit.label, entity.name, batch_id, n)
    return out


def ingest_all(con: duckdb.DuckDBPyConnection, sources: Iterable[Source],
               entities: Optional[Iterable[Entity]] = None) -> dict:
    totals = {}
    for e in (entities or ENTITIES):
        t = {"batches": 0, "rows": 0, "skipped": 0}
        for s in sources:
            r = ingest_entity(con, s, e)
            for k in t:
                t[k] += r[k]
        totals[e.name] = t
    return totals
