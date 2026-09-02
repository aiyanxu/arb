# syntax=docker/dockerfile:1
# entropy-arb — two-venue perp arbitrage bot.
#
# Two targets (docker build --target <name> .):
#   record-only — base deps only; --record-only data collection needs no SDKs
#   live        — + hyperliquid-python-sdk, eth-account, lighter-sdk (git)
#
# Nothing secret is baked in: config.yaml / symbol_map.yaml / .env are
# mounted at runtime (see .dockerignore). Logs land in /app/logs (volume).

########### builder: base deps (shared by both targets) ###########
FROM python:3.12-slim AS builder-record
# git is only needed for the lighter-sdk git dependency, but keeping it in
# the shared builder keeps both targets identical up to the extras step.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
# README.md and LICENSE are required by the pyproject metadata
COPY pyproject.toml README.md LICENSE ./
COPY entropy_arb/ entropy_arb/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install /app

########### builder: live extras (signing SDKs) ###########
FROM builder-record AS builder-live
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "/app[live]"

########### runtime: shared layout ###########
FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 1000 arb \
 && mkdir -p /app/logs \
 && chown -R arb:arb /app
WORKDIR /app
COPY --chown=arb:arb tools/ tools/
USER arb

########### final: slim, record-only image ###########
FROM runtime AS record-only
COPY --from=builder-record /opt/venv /opt/venv
# safe default: data collection only — pass flags to change behavior
ENTRYPOINT ["entropy-arb"]
CMD ["--record-only", "--no-dashboard"]

########### final: full image with signing SDKs ###########
FROM runtime AS live
COPY --from=builder-live /opt/venv /opt/venv
ENTRYPOINT ["entropy-arb"]
CMD ["--record-only", "--no-dashboard"]
