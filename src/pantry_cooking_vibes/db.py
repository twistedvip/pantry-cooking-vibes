import csv
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path

# schema.sql, migrations/, and canonical_seed.csv live inside the package
# as importable assets so they ship in the wheel and stay resolvable when
# pantry-cooking-vibes is installed as a dependency (no source tree assumed).
_PKG_DIR = Path(__file__).parent
_ASSETS_DIR = _PKG_DIR / "_assets"
# Runtime DB lives in the consumer's working tree, not the package. Resolved
# from CWD so `meal-cli` and the web app land on the same data/app.db a user
# expects to see at their project root.
DB_PATH = Path.cwd() / "data" / "app.db"
SCHEMA_PATH = _ASSETS_DIR / "schema.sql"
SEED_PATH = _ASSETS_DIR / "canonical_seed.csv"

# Env var: os.pathsep-separated extra roots permitted for the runtime DB.
# Used by operators who deploy the DB outside the project tree (e.g.
# /var/lib/meal-planner). Empty by default — only the project ./data root
# and the OS tempdir (for tests) are allowed.
_DB_ROOT_ENV = "PANTRY_COOKING_VIBES_DB_ROOT"


def _root_prefix(p: str) -> str:
    """Suffix a realpath'd directory with ``os.sep`` so ``startswith`` won't
    accept ``/data2`` for ``/data``. Idempotent."""
    return p if p.endswith(os.sep) else p + os.sep


# Resolved once at import as string constants — CodeQL's sanitizer engine
# recognizes ``startswith(<constant>)`` on a realpath'd value as a
# SafeAccessCheck barrier that clears the path-injection taint state.
_DATA_ROOT_PREFIX: str = _root_prefix(os.path.realpath(str(DB_PATH.parent)))
try:
    _TMP_ROOT_PREFIX: str = _root_prefix(os.path.realpath(tempfile.gettempdir()))
except OSError:
    _TMP_ROOT_PREFIX = _DATA_ROOT_PREFIX  # degenerate: fall back to data root


def _env_root_prefixes() -> tuple[str, ...]:
    """Operator-supplied extra roots via ``PANTRY_COOKING_VIBES_DB_ROOT``."""
    extra = os.environ.get(_DB_ROOT_ENV)
    if not extra:
        return ()
    out: list[str] = []
    for part in extra.split(os.pathsep):
        part = part.strip()
        if part:
            with suppress(OSError):
                out.append(_root_prefix(os.path.realpath(os.path.expanduser(part))))
    return tuple(out)


_ENV_ROOT_PREFIXES: tuple[str, ...] = _env_root_prefixes()


def _resolve_safe_db_path(db_path: Path | str | None) -> str:
    """Validate and canonicalize ``db_path`` before any filesystem access.

    CodeQL ``py/path-injection`` sanitizer barrier:
    - ``os.path.realpath`` (PathNormalization → state ``NormalizedUnchecked``)
    - ``startswith`` against module-level realpath'd constant prefix
      (SafeAccessCheck → state cleared on true branch)

    Returns the canonical path string. Raises ``ValueError`` otherwise.
    """
    if db_path is None:
        raise ValueError("db_path must not be None")
    raw = os.fspath(db_path)
    if not raw:
        raise ValueError("db_path must not be empty")
    if "\x00" in raw:
        raise ValueError("db_path must not contain NUL byte")

    candidate = os.path.realpath(os.path.expanduser(raw))

    if candidate.startswith(_DATA_ROOT_PREFIX) or candidate.startswith(_TMP_ROOT_PREFIX):
        return candidate
    for env_prefix in _ENV_ROOT_PREFIXES:
        if candidate.startswith(env_prefix):
            return candidate

    raise ValueError(
        f"db_path escapes the allowed roots; set {_DB_ROOT_ENV} to permit deployment outside ./data"
    )


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Return a new connection with sensible defaults."""
    # Inline CodeQL py/path-injection sanitizer barrier. Pattern: realpath
    # (PathNormalization) + startswith against a module-level constant
    # prefix (SafeAccessCheck on true branch). All filesystem access lives
    # inside the true branch so the cleared-taint state applies.
    raw = os.fspath(db_path)
    if not raw or "\x00" in raw:
        raise ValueError("invalid db_path")
    candidate = os.path.realpath(os.path.expanduser(raw))

    # Each branch holds its own `startswith` so CodeQL recognizes each as a
    # distinct SafeAccessCheck barrier (combining via `or` works too but
    # keeping them split makes the dataflow intent explicit).
    safe_path: str | None = None
    if candidate.startswith(_DATA_ROOT_PREFIX):  # noqa: SIM114
        safe_path = candidate
    elif candidate.startswith(_TMP_ROOT_PREFIX):
        safe_path = candidate
    else:
        for env_prefix in _ENV_ROOT_PREFIXES:
            if candidate.startswith(env_prefix):
                safe_path = candidate
                break

    if safe_path is None:
        raise ValueError("db_path outside allowed roots")

    Path(safe_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(safe_path)
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


_MIGRATIONS_DIR = _ASSETS_DIR / "migrations"
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
    safe_str = _resolve_safe_db_path(db_path)
    safe_path = Path(safe_str)
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(safe_path) as conn:
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
