FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/pilot107/src

RUN groupadd --gid 10700 pilot107 \
    && useradd --uid 10700 --gid pilot107 --home-dir /var/lib/pilot107 --create-home --shell /usr/sbin/nologin pilot107 \
    && mkdir -p /opt/pilot107/src /var/lib/pilot107 \
    && chown -R pilot107:pilot107 /var/lib/pilot107

WORKDIR /opt/pilot107

COPY pyproject.toml README.md ./
RUN python3 -c 'import subprocess, sys, tomllib; dependencies = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]; subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", *dependencies])'

COPY src ./src
COPY data/known_errors ./data/known_errors
COPY data/submission_templates ./data/submission_templates
COPY config/platform_profiles ./config/platform_profiles

USER 10700:10700

CMD ["python3", "-m", "pilot107.api.dev_server", "--db-path", "/var/lib/pilot107/pilot107.db", "--evidence-root", "/var/lib/pilot107/evidence", "--host", "0.0.0.0", "--port", "8080"]
