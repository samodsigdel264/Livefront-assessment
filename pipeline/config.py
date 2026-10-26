"""Pipeline settings. Everything business-specific that we *assumed* lives here, in one place."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = Path(__file__).resolve().parent / "sql"


@dataclass(frozen=True)
class Settings:
    db_path: Path = ROOT / "warehouse" / "carton_caps.duckdb"
    export_dir: Path = ROOT / "warehouse" / "export"

    # --- business assumptions (see docs/DATA_ASSUMPTIONS.md) -------------------------------
    # The source gives "points" but no points->dollars rate. 1 point = 1 cent is an assumption.
    point_value_usd: float = 0.01
    # Referral rules PDF: "$5 or equivalent" for referrer and for the referred user.
    referral_bonus_usd: float = 5.0
    # PDF: bonus requires "a qualifying action (e.g. first eligible product scan)".
    # Our only event stream is purchases, so qualifying action = first purchase within N days.
    qualify_window_days: int = 7

    # --- data-quality circuit breaker -----------------------------------------------------
    # If more than this share of a *new* batch is rejected, treat it as a systemic break
    # (bad deploy upstream, wrong file) and stop before publishing, instead of quietly
    # quarantining half the data.
    max_reject_rate: float = 0.10
    # ...but a rate over a handful of rows is noise (1 bad row in a 5-row correction file = 20%),
    # so the breaker only arms once a batch has at least this many new rows.
    breaker_min_rows: int = 50

    # --- layout ---------------------------------------------------------------------------
    gold_tables_to_export: tuple = field(default_factory=tuple)  # empty = all gold tables

    def sql_params(self) -> dict:
        return {
            "POINT_VALUE_USD": repr(self.point_value_usd),
            "REFERRAL_BONUS_USD": repr(self.referral_bonus_usd),
            "QUALIFY_WINDOW_DAYS": str(int(self.qualify_window_days)),
        }
