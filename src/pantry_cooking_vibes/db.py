import csv
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Resolve paths relative to project root (two levels above this file's src/pantry_cooking_vibes/)
_PROJECT_ROOT = Path(__file__).parent.parent.parent
DB_PATH = _PROJECT_ROOT / "data" / "app.db"
SCHEMA_PATH = _PROJECT_ROOT / "db" / "schema.sql"
SEED_PATH = _PROJECT_ROOT / "data" / "seed" / "canonical_seed.csv"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Return a new connection with sensible defaults."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def connect(db_path: Path = DB_PATH):
    """Context manager that yields a connection and commits on clean exit."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_schema(conn: sqlite3.Connection, schema_path: Path = SCHEMA_PATH) -> None:
    """Execute schema.sql against an open connection (idempotent — uses CREATE IF NOT EXISTS)."""
    sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(sql)


def seed_canonical_ingredients(conn: sqlite3.Connection, seed_path: Path = SEED_PATH) -> int:
    """Load canonical_seed.csv into canonical_ingredients. Returns number of rows inserted."""
    if not seed_path.exists():
        return 0

    inserted = 0
    with seed_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            cur = conn.execute(
                """
                INSERT INTO canonical_ingredients (name, category, default_unit, aliases)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO NOTHING
                """,
                (
                    name.lower(),
                    (row.get("category") or "").strip() or None,
                    (row.get("default_unit") or "").strip() or None,
                    (row.get("aliases") or "[]").strip(),
                ),
            )
            inserted += cur.rowcount
    return inserted


_MIGRATIONS_DIR = _PROJECT_ROOT / "db" / "migrations"
_MIGRATION_VERSION_RE = re.compile(r"^(\d+)_")


def _migration_version(filename: str) -> int:
    """Extract the leading numeric version from a migration filename like '003_foo.sql'."""
    m = _MIGRATION_VERSION_RE.match(filename)
    if not m:
        raise ValueError(
            f"migration filename {filename!r} must start with NNN_ (e.g. '003_add_column.sql')"
        )
    return int(m.group(1))


def _get_user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA user_version doesn't accept ? placeholders; version is an int we control.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def run_migrations(conn: sqlite3.Connection, migrations_dir: Path = _MIGRATIONS_DIR) -> list[str]:
    """Apply any *.sql migration files whose numeric version > ``PRAGMA user_version``.

    Authoritative order is the leading ``NNN_`` prefix on each filename. Filenames
    are still recorded in ``schema_migrations`` for human history. Legacy DBs
    that pre-date ``user_version`` tracking auto-sync from the recorded rows on
    first run. Returns list of newly applied filenames.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    recorded = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}

    # Legacy sync: a DB with rows in schema_migrations but user_version=0 must be
    # bumped to the highest recorded version so we don't re-apply old migrations.
    current_version = _get_user_version(conn)
    if current_version == 0 and recorded:
        recorded_max = max(_migration_version(f) for f in recorded)
        _set_user_version(conn, recorded_max)
        current_version = recorded_max

    ran: list[str] = []
    if not migrations_dir.exists():
        return ran

    files = sorted(migrations_dir.glob("*.sql"), key=lambda p: _migration_version(p.name))
    for sql_file in files:
        version = _migration_version(sql_file.name)
        if version <= current_version:
            continue
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (sql_file.name,))
        _set_user_version(conn, version)
        current_version = version
        ran.append(sql_file.name)
    return ran


def init_db(db_path: Path = DB_PATH) -> int:
    """Create data dir, apply schema, run migrations, seed canonical ingredients. Returns seed row count."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        apply_schema(conn)
        run_migrations(conn)
        count = seed_canonical_ingredients(conn)
    return count


# ---------------------------------------------------------------------------
# Generic query helpers
# ---------------------------------------------------------------------------


def fetchall(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def fetchone(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def execute(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    return conn.execute(sql, params)
