# python:3.12-slim, pinned 2026-07-21 (digest pin for reproducible builds;
# the readable tag is kept here only as a comment for discoverability).
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/pilot107/src

RUN groupadd --gid 10700 pilot107 \
    && useradd --uid 10700 --gid pilot107 --home-dir /var/lib/pilot107 --create-home --shell /usr/sbin/nologin pilot107 \
    && mkdir -p /opt/pilot107/src /var/lib/pilot107 \
    && chown -R pilot107:pilot107 /var/lib/pilot107

WORKDIR /opt/pilot107

# Install uv as a single small tool. Chosen over multi-stage copy from
# ghcr.io/astral-sh/uv because that image is itself a floating tag and we'd
# have to fetch+pin its digest; pip-installing uv is a one-time small tool
# install and uv itself then resolves every runtime dep strictly from uv.lock
# (the source of truth), so the floating uv version does not affect which
# application dependency versions land in the image.
RUN pip install --no-cache-dir uv

# Copy the lock + project metadata BEFORE src so the dependency layer caches
# separately (deps change less often than src). uv.lock is the source of truth;
# pyproject.toml is needed for project metadata during export.
COPY pyproject.toml uv.lock README.md ./

# Export the locked runtime deps (no dev, project itself omitted — it is
# COPY'd as src/ afterward) and install them into the SYSTEM Python so the
# existing CMD (`python3 -m pilot107.api.dev_server`) works without a venv
# activation wrapper. --frozen guarantees uv.lock is used as-is (no resolver
# drift); --no-cache-dir keeps the layer small.
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --system --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY src ./src
COPY data/known_errors ./data/known_errors
COPY data/submission_templates ./data/submission_templates
COPY config/platform_profiles ./config/platform_profiles

USER 10700:10700

CMD ["python3", "-m", "pilot107.api.dev_server", "--db-path", "/var/lib/pilot107/pilot107.db", "--evidence-root", "/var/lib/pilot107/evidence", "--host", "0.0.0.0", "--port", "8080"]
