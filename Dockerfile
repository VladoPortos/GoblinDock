# GoblinDock — single-container Proxmox control panel (FastAPI + worker + SPA).
# Base pinned by digest for reproducibility (python:3.12-slim at build time).
FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9 AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    GOBLINDOCK_DATA_DIR=/data

# ca-certificates for TLS; openssh-client because ansible shells out to `ssh` to
# reach VMs; libnss-wrapper fakes a passwd entry for the arbitrary runtime uid so
# ssh doesn't bail with "No user exists for uid". SPA libs are committed under
# web/vendor (no build-time downloads).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssh-client libnss-wrapper \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# --require-hashes: refuse to install anything whose artifact hash isn't pinned in
# requirements.txt (supply-chain integrity — a compromised index can't swap a wheel).
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Ansible collections used by the pre-built blocks. Installed to a system path so
# they resolve regardless of the runtime uid/HOME (ANSIBLE_COLLECTIONS_PATH default).
COPY collections/requirements.yml ./collections/requirements.yml
RUN ansible-galaxy collection install -r collections/requirements.yml -p /usr/share/ansible/collections

# Application + static SPA (React is vendored under web/vendor — no CDN).
COPY app ./app
COPY web ./web
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Non-root by default. Runtime can override the uid (compose `user:`) to match a
# bind-mounted /data + SSH key on the host. /home/appuser is world-writable so an
# overridden uid still has a writable HOME (ssh/ansible scratch space).
RUN useradd -r -u 10001 -m -d /home/appuser appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app \
    && chmod -R a+rX /app \
    && chmod 0777 /home/appuser \
    && chmod +x /usr/local/bin/docker-entrypoint.sh
USER appuser

ENV HOME=/home/appuser

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else sys.exit(1)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
