# syntax=docker/dockerfile:1.7
# 既存の同一revisionイメージを使う場合だけ、build引数で置き換える。
ARG IRODORI_UPSTREAM_IMAGE=upstream
FROM python:3.12-slim AS upstream
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates ffmpeg git libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
WORKDIR /app
RUN git init . && git remote add origin https://github.com/Aratako/Irodori-TTS-Server.git \
    && git fetch --depth 1 origin 841fb7c6ec57729c56b9b75c0ef2562249b13a10 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 841fb7c6ec57729c56b9b75c0ef2562249b13a10 \
    && grep -q 8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71 uv.lock
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --locked --no-dev --no-editable --extra cu128

FROM ${IRODORI_UPSTREAM_IMAGE} AS service
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY irodori_service /app/irodori_service
LABEL org.opencontainers.image.source="https://github.com/FYuki/digital-souls" \
    org.digital-souls.irodori-server-revision="841fb7c6ec57729c56b9b75c0ef2562249b13a10" \
    org.digital-souls.irodori-revision="8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71"
EXPOSE 50024
ENTRYPOINT ["/app/.venv/bin/python", "-m", "uvicorn", "irodori_service.app:app"]
CMD ["--host", "127.0.0.1", "--port", "50024", "--workers", "1", "--no-access-log"]
