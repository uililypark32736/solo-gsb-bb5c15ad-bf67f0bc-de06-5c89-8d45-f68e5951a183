"""SQLite connection management and schema initialisation."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('validate', 'trial', 'best_adjacent_swap', 'joint_trial')),
    created_at  TEXT NOT NULL,
    matrix_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flights (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway        TEXT NOT NULL,
    position      INTEGER NOT NULL,
    flight_no     TEXT NOT NULL,
    scheduled_off TEXT NOT NULL,
    wake          TEXT NOT NULL,
    locked        INTEGER,
    UNIQUE(batch_id, runway, position)
);

CREATE TABLE IF NOT EXISTS violations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway            TEXT NOT NULL,
    leader_position   INTEGER NOT NULL,
    follower_position INTEGER NOT NULL,
    leader_no         TEXT NOT NULL,
    follower_no       TEXT NOT NULL,
    leader_wake       TEXT NOT NULL,
    follower_wake     TEXT NOT NULL,
    leader_time       TEXT NOT NULL,
    follower_time     TEXT NOT NULL,
    required_seconds  INTEGER NOT NULL,
    actual_seconds    INTEGER NOT NULL,
    shortfall_seconds INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trial_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway         TEXT NOT NULL,
    position       INTEGER NOT NULL,
    runway_position INTEGER,
    flight_no      TEXT NOT NULL,
    wake           TEXT NOT NULL,
    scheduled_off  TEXT NOT NULL,
    adjusted_off   TEXT NOT NULL,
    delay_seconds  INTEGER NOT NULL,
    constraint_type TEXT,
    triggered_by   TEXT,
    closure_hit    INTEGER NOT NULL DEFAULT 0,
    closure_window_start TEXT,
    closure_window_end   TEXT,
    locked         INTEGER NOT NULL DEFAULT 0,
    capacity_hit   INTEGER NOT NULL DEFAULT 0,
    capacity_window_seconds INTEGER,
    capacity_max_departures INTEGER,
    capacity_anchor TEXT,
    capacity_window_start TEXT,
    capacity_window_end   TEXT,
    mutex_group_hit INTEGER NOT NULL DEFAULT 0,
    mutex_group_name TEXT,
    mutex_group_anchor TEXT,
    mutex_group_min_interval_seconds INTEGER,
    mutex_group_window_start TEXT,
    mutex_group_window_end   TEXT
);

CREATE TABLE IF NOT EXISTS closures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway       TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    start_utc    TEXT NOT NULL,
    end_utc      TEXT NOT NULL,
    UNIQUE(batch_id, runway, window_index)
);

CREATE TABLE IF NOT EXISTS closure_conflicts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway        TEXT NOT NULL,
    position      INTEGER NOT NULL,
    flight_no     TEXT NOT NULL,
    wake          TEXT NOT NULL,
    scheduled_off TEXT NOT NULL,
    window_index  INTEGER NOT NULL,
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capacities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway         TEXT NOT NULL,
    window_seconds INTEGER NOT NULL,
    max_departures INTEGER NOT NULL,
    UNIQUE(batch_id, runway)
);

CREATE TABLE IF NOT EXISTS capacity_violations (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id               TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway                 TEXT NOT NULL,
    position               INTEGER NOT NULL,
    flight_no              TEXT NOT NULL,
    wake                   TEXT NOT NULL,
    scheduled_off          TEXT NOT NULL,
    window_seconds         INTEGER NOT NULL,
    max_departures         INTEGER NOT NULL,
    departures_in_window   INTEGER NOT NULL,
    anchor_flight_no       TEXT NOT NULL,
    anchor_position        INTEGER NOT NULL,
    window_start           TEXT NOT NULL,
    window_end             TEXT NOT NULL,
    earliest_compliant_off TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trial_blockers (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id               TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway                 TEXT NOT NULL,
    position               INTEGER NOT NULL,
    flight_no              TEXT NOT NULL,
    wake                   TEXT NOT NULL,
    scheduled_off          TEXT NOT NULL,
    constraint_type        TEXT NOT NULL,
    required_earliest_off  TEXT NOT NULL,
    triggered_by           TEXT,
    required_gap_seconds   INTEGER,
    closure_hit            INTEGER NOT NULL DEFAULT 0,
    closure_window_start   TEXT,
    closure_window_end     TEXT,
    capacity_hit           INTEGER NOT NULL DEFAULT 0,
    capacity_window_seconds   INTEGER,
    capacity_max_departures   INTEGER,
    capacity_anchor        TEXT,
    capacity_window_start  TEXT,
    capacity_window_end    TEXT,
    mutex_group_hit        INTEGER NOT NULL DEFAULT 0,
    mutex_group_name       TEXT,
    mutex_group_anchor     TEXT,
    mutex_group_min_interval_seconds INTEGER,
    mutex_group_window_start TEXT,
    mutex_group_window_end   TEXT,
    required_times_json    TEXT,
    UNIQUE(batch_id, runway)
);

CREATE TABLE IF NOT EXISTS swap_results (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id                      TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway                        TEXT NOT NULL,
    baseline_order_json           TEXT NOT NULL,
    swap_position                 INTEGER,
    swap_first_flight_no          TEXT,
    swap_second_flight_no         TEXT,
    baseline_delayed_count        INTEGER NOT NULL,
    baseline_total_delay_seconds  INTEGER NOT NULL,
    baseline_max_delay_seconds    INTEGER NOT NULL,
    saved_seconds                 INTEGER NOT NULL,
    UNIQUE(batch_id, runway)
);

CREATE TABLE IF NOT EXISTS mutex_groups (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id             TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    group_index          INTEGER NOT NULL,
    name                 TEXT NOT NULL,
    runways_json         TEXT NOT NULL,
    min_interval_seconds INTEGER NOT NULL,
    UNIQUE(batch_id, group_index)
);

CREATE INDEX IF NOT EXISTS idx_flights_batch ON flights(batch_id);
CREATE INDEX IF NOT EXISTS idx_violations_batch ON violations(batch_id);
CREATE INDEX IF NOT EXISTS idx_trial_batch ON trial_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_closures_batch ON closures(batch_id);
CREATE INDEX IF NOT EXISTS idx_closure_conflicts_batch ON closure_conflicts(batch_id);
CREATE INDEX IF NOT EXISTS idx_capacities_batch ON capacities(batch_id);
CREATE INDEX IF NOT EXISTS idx_capacity_violations_batch ON capacity_violations(batch_id);
CREATE INDEX IF NOT EXISTS idx_trial_blockers_batch ON trial_blockers(batch_id);
CREATE INDEX IF NOT EXISTS idx_swap_results_batch ON swap_results(batch_id);
CREATE INDEX IF NOT EXISTS idx_mutex_groups_batch ON mutex_groups(batch_id);
"""

# Columns added after the initial release: existing database files (e.g. the
# Docker volume) are migrated in place, since CREATE TABLE IF NOT EXISTS never
# alters an already-present table.
_FLIGHT_COLUMNS = {
    # NULL = the request never carried ``locked``; 0/1 = explicitly given.
    "locked": "INTEGER",
}

_TRIAL_ITEM_COLUMNS = {
    "closure_hit": "INTEGER NOT NULL DEFAULT 0",
    "closure_window_start": "TEXT",
    "closure_window_end": "TEXT",
    # rolling-capacity support
    "capacity_hit": "INTEGER NOT NULL DEFAULT 0",
    "capacity_window_seconds": "INTEGER",
    "capacity_max_departures": "INTEGER",
    "capacity_anchor": "TEXT",
    "capacity_window_start": "TEXT",
    "capacity_window_end": "TEXT",
    # immovable (locked) flight support
    "locked": "INTEGER NOT NULL DEFAULT 0",
}


def _rebuild_batches_if_needed(conn: sqlite3.Connection) -> None:
    """Rebuild ``batches`` when its ``mode`` CHECK predates new modes.

    Databases created before the best-adjacent-swap release constrain
    ``mode`` to ``('validate', 'trial')``; SQLite cannot alter a CHECK, so
    the table is rebuilt in place (rows preserved) using the canonical
    create-new / copy / drop-old / rename sequence. Foreign keys are
    disabled for the duration so dropping the old parent table neither
    cascades into child tables nor leaves their REFERENCES clauses
    rewritten (a plain ``ALTER TABLE ... RENAME`` would retarget them).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'batches'"
    ).fetchone()
    if row is None or "joint_trial" in row[0]:
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS batches_new")
        conn.execute(
            "CREATE TABLE batches_new ("
            " batch_id TEXT PRIMARY KEY,"
            " mode TEXT NOT NULL"
            " CHECK (mode IN ('validate', 'trial', 'best_adjacent_swap', 'joint_trial')),"
            " created_at TEXT NOT NULL,"
            " matrix_json TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO batches_new (batch_id, mode, created_at, matrix_json)"
            " SELECT batch_id, mode, created_at, matrix_json FROM batches"
        )
        conn.execute("DROP TABLE batches")
        conn.execute("ALTER TABLE batches_new RENAME TO batches")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


_TRIAL_ITEM_JOINT_COLUMNS = {
    # columns added for intersecting-runway joint trials
    "runway_position": "INTEGER",
    # first constraint that forced the push (joint trials; NULL otherwise)
    "constraint_type": "TEXT",
    "mutex_group_hit": "INTEGER NOT NULL DEFAULT 0",
    "mutex_group_name": "TEXT",
    "mutex_group_anchor": "TEXT",
    "mutex_group_min_interval_seconds": "INTEGER",
    "mutex_group_window_start": "TEXT",
    "mutex_group_window_end": "TEXT",
}

_TRIAL_BLOCKER_JOINT_COLUMNS = {
    # first infeasible locked flight of a joint trial also states its runway,
    # the mutex-group detail and every constraint's required time (JSON).
    "mutex_group_hit": "INTEGER NOT NULL DEFAULT 0",
    "mutex_group_name": "TEXT",
    "mutex_group_anchor": "TEXT",
    "mutex_group_min_interval_seconds": "INTEGER",
    "mutex_group_window_start": "TEXT",
    "mutex_group_window_end": "TEXT",
    "required_times_json": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    _rebuild_batches_if_needed(conn)
    flight_cols = {row[1] for row in conn.execute("PRAGMA table_info(flights)")}
    for name, ddl in _FLIGHT_COLUMNS.items():
        if name not in flight_cols:
            conn.execute(f"ALTER TABLE flights ADD COLUMN {name} {ddl}")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trial_items)")}
    for name, ddl in {**_TRIAL_ITEM_COLUMNS, **_TRIAL_ITEM_JOINT_COLUMNS}.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE trial_items ADD COLUMN {name} {ddl}")
    blocker_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(trial_blockers)")
    }
    for name, ddl in _TRIAL_BLOCKER_JOINT_COLUMNS.items():
        if name not in blocker_cols:
            conn.execute(f"ALTER TABLE trial_blockers ADD COLUMN {name} {ddl}")


def _connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create the schema (idempotent). Called once at application startup."""
    path = db_path or settings.db_path
    with _connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Transactional connection: commits on success, rolls back on error."""
    conn = _connect(db_path or settings.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency."""
    with get_conn() as conn:
        yield conn
