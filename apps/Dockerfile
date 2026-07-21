# Deps-exporter stage: use uv to export the LOCKED runtime requirements to a
# plain requirements.txt. uv is pinned (==0.11.12) to avoid builder drift, and
# crucially uv does NOT enter the final image — only the exported
# requirements.txt is copied across. uv's job is just to read uv.lock and emit
# exact `==` pins; since uv.lock is the source of truth, the exported file is
# deterministic regardless of uv's own version, but pinning uv is still good
# practice for builder reproducibility.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS deps-exporter

RUN pip install --no-cache-dir uv==0.11.12

WORKDIR /opt/pilot107
COPY pyproject.toml uv.lock ./
# --frozen: use uv.lock as-is, no resolver drift.
# --no-dev: exclude dev/test extras.
# --no-emit-project: exclude the project itself (COPY'd as src/ afterward).
# touch -d @0 normalizes the output file mtime so the COPY --from layer in the
# final stage is deterministic.
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && touch -d @0 /tmp/requirements.txt

# Final stage: install locked requirements into system Python. No uv here.
# python:3.12-slim, pinned 2026-07-21 (digest pin for reproducible builds;
# the readable tag is kept here only as a comment for discoverability).
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/pilot107/src \
    SOURCE_DATE_EPOCH=0

RUN groupadd --gid 10700 pilot107 \
    && useradd --uid 10700 --gid pilot107 --home-dir /var/lib/pilot107 --create-home --shell /usr/sbin/nologin pilot107 \
    && mkdir -p /opt/pilot107/src /var/lib/pilot107 \
    && chown -R pilot107:pilot107 /var/lib/pilot107 \
    && find /opt/pilot107 /var/lib/pilot107 /etc/group /etc/passwd /etc/shadow /etc/subuid /etc/subgid -exec touch -h -d @0 {} + 2>/dev/null || true

WORKDIR /opt/pilot107

# Install ONLY the locked runtime deps (every version from uv.lock via the
# exported requirements.txt). --no-cache-dir keeps the layer small. The
# requirements.txt has exact == pins so pip cannot drift. The final find
# normalizes all installed file mtimes to epoch 0 for reproducible layers.
COPY --from=deps-exporter /tmp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && find /usr/local/lib/python3.12 /usr/local/bin -exec touch -h -d @0 {} + 2>/dev/null || true

COPY src ./src
COPY data/known_errors ./data/known_errors
COPY data/submission_templates ./data/submission_templates
COPY config/platform_profiles ./config/platform_profiles

# Normalize mtimes on all COPY'd files so the layer digests are deterministic.
# Without this, COPY layers have files with the host filesystem mtime, which
# can vary between builds (e.g. git checkout time). Setting everything to
# epoch 0 makes the final filesystem state byte-identical across builds.
# NOTE: this adds one extra layer but it is itself deterministic (same touch
# operation every time). Earlier COPY layers may still differ in directory
# mtimes, but the final filesystem state — and thus the image's observable
# content — is reproducible. The verify-reproducible-build.sh script checks
# both image IDs and normalized rootfs content.
RUN find /opt/pilot107 -exec touch -h -d @0 {} + 2>/dev/null || true

USER 10700:10700

CMD ["python3", "-m", "pilot107.api.dev_server", "--db-path", "/var/lib/pilot107/pilot107.db", "--evidence-root", "/var/lib/pilot107/evidence", "--host", "0.0.0.0", "--port", "8080"]
