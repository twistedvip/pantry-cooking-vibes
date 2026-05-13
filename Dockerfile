# syntax=docker/dockerfile:1.7
#
# Multi-stage build for pantry-cooking-vibes (meal-cli).
#
# Stage 1 (builder): installs project deps into an isolated /opt/venv via uv.
# Stage 2 (runtime): slim image carrying only the venv + runtime artifacts
# (db schema, seed data, entrypoint). No build tools, no uv, no pip cache.
#
# Build:   docker build -t pantry-cooking-vibes .
# Run:     docker run -p 8000:8000 -v meal_data:/app/data/store pantry-cooking-vibes
#
# Override port:    -e PORT=9000 -p 9000:9000
# Override command: append e.g. `meal-cli list-sources` after the image name.

# -------------------------------------------------------------------
# Stage 1 — builder
# -------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN pip install --no-cache-dir uv

# schema.sql, migrations/, and canonical_seed.csv are packaged under
# src/pantry_cooking_vibes/_assets/, so only src/ needs to be copied — the
# editable install's .pth file points at /app/src and the runtime stage
# inherits the same path.
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# --no-dev skips [dev]/[e2e] extras (~580MB savings vs full install).
RUN uv sync --locked --no-dev

# -------------------------------------------------------------------
# Stage 2 — runtime
# -------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    PANTRY_COOKING_VIBES_DB=/app/data/store/app.db

# Non-root user for security. UID/GID 1000 = typical first-user mapping;
# host bind-mounts owned by uid 1000 will Just Work.
RUN groupadd --system --gid 1000 appuser \
 && useradd --system --uid 1000 --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser

WORKDIR /app

# Venv from builder. Editable install's .pth file inside this venv references
# /app/src — that path must exist in runtime too, hence the /app COPY below.
COPY --from=builder /opt/venv /opt/venv

# Whole project tree from builder. Carries src/ (editable .pth target +
# packaged _assets/ holding schema.sql, migrations/, canonical_seed.csv);
# pyproject.toml + uv.lock + README come along, ~10KB total.
COPY --from=builder /app /app
COPY docker/entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh \
 && mkdir -p /app/data/store \
 && chown -R appuser:appuser /app

USER appuser

# Persistent volume scoped to the SQLite store only. /app/data/store/ is the
# sole writable surface; the rest of /app/data is unused now that seed assets
# live inside the installed package.
VOLUME ["/app/data/store"]

# Documented default port. Override with -e PORT=NNNN at runtime; the CMD
# below picks up $PORT, so EXPOSE here is informational only.
EXPOSE 8000

# Probe /healthz (cheap plain-text route — no template render, no DB query).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=3).status==200 else 1)" \
  || exit 1

# entrypoint runs first-run init (db-init + demo ingest if DB missing),
# then execs whatever the CMD / `docker run ... <cmd>` resolves to.
ENTRYPOINT ["/app/entrypoint.sh"]

# Default: web UI on 0.0.0.0:$PORT. The DB path comes from
# $PANTRY_COOKING_VIBES_DB (set above) — Typer reads it via envvar=,
# so explicit --db is unnecessary. Shell-form CMD lets $PORT expand at
# container start. Override entirely with `command:` in compose, e.g.
# for serve-mcp.
CMD ["sh", "-c", "exec meal-cli serve-web --host 0.0.0.0 --port ${PORT:-8000}"]
