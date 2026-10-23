# IB Gateway + IBC (from the maintained gnzsnz image) and the execution service in one
# container, so the gateway's API port (4002 paper / 4001 live) never leaves localhost.
#
# Build from the repo root:  docker build -f deploy/Dockerfile -t ibkr-exec .
FROM ghcr.io/gnzsnz/ib-gateway:stable

USER root
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl netcat-openbsd tini \
 && rm -rf /var/lib/apt/lists/*

# uv brings its own CPython 3.12 build; the base image has no Python.
COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv python install 3.12 && uv sync --frozen --no-dev --no-install-project

COPY ibkr_exec ./ibkr_exec
COPY alembic ./alembic
COPY alembic.ini config.yaml ./
COPY deploy/start.sh /app/start.sh
RUN uv sync --frozen --no-dev && chmod +x /app/start.sh \
 && chown -R 1000:1000 /app /opt/venv \
 && rm -rf /root/.cache/uv "${HOME}/.cache/uv"   # build caches are root-owned; drop them

# Back to the gateway's user (uid 1000, HOME=/home/ibgateway) so run.sh works unchanged.
USER 1000:1000
ENV HOME=/home/ibgateway \
    TRADING_MODE=paper \
    READ_ONLY_API=no \
    BYPASS_WARNING=yes \
    TWOFA_TIMEOUT_ACTION=restart \
    RELOGIN_AFTER_TWOFA_TIMEOUT=yes \
    AUTO_RESTART_TIME="12:30 AM" \
    TIME_ZONE=America/New_York \
    TWS_ACCEPT_INCOMING=accept \
    EXISTING_SESSION_DETECTED_ACTION=primary \
    IB_HOST=127.0.0.1 \
    IB_PORT=4002 \
    BROKER=ib \
    HOST=0.0.0.0 \
    PORT=8000 \
    LOG_FORMAT=json

# The gateway API binds to 127.0.0.1 inside the container, so "accept" only ever applies
# to this container's own processes; start.sh also stops the socat relay on 4004.
# 8000 is reached only over Render's private network; 4001/4002 stay inside.
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
