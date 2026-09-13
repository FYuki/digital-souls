# syntax=docker/dockerfile:1.7
# 既存の同一revisionイメージを使う場合だけ、build引数で置き換える。
ARG IRODORI_UPSTREAM_IMAGE=upstream
FROM python:3.12-slim AS upstream
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv UV_PYTHON_INSTALL_DIR=/opt/uv-python
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
USER root
RUN (getent group 10001 >/dev/null || groupadd --gid 10001 irodori) \
    && (getent passwd 10001 >/dev/null || useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin irodori) \
    && install -d -m 0755 -o 10001 -g 10001 /home/irodori
# 旧イメージをbuild引数に使う場合も、root専用領域にあるuv管理Pythonを移す。
RUN if [ -d /root/.local/share/uv/python ]; then \
      mv /root/.local/share/uv/python /opt/uv-python; \
      for runtime in /opt/uv-python/*; do \
        if [ -L "$runtime" ]; then \
          ln -sfn "$(readlink "$runtime" | sed 's|/root/.local/share/uv/python|/opt/uv-python|')" "$runtime"; \
        fi; \
      done; \
      ln -sfn "$(readlink /app/.venv/bin/python | sed 's|/root/.local/share/uv/python|/opt/uv-python|')" /app/.venv/bin/python; \
      sed -i 's|/root/.local/share/uv/python|/opt/uv-python|g' /app/.venv/pyvenv.cfg; \
    fi
ENV HOME=/home/irodori UV_PYTHON_INSTALL_DIR=/opt/uv-python
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY irodori_service /app/irodori_service
LABEL org.opencontainers.image.source="https://github.com/FYuki/digital-souls" \
    org.digital-souls.irodori-server-revision="841fb7c6ec57729c56b9b75c0ef2562249b13a10" \
    org.digital-souls.irodori-revision="8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71"
USER 10001:10001
EXPOSE 50024
ENTRYPOINT ["/app/.venv/bin/python", "-m", "uvicorn", "irodori_service.app:app"]
CMD ["--host", "127.0.0.1", "--port", "50024", "--workers", "1", "--no-access-log"]
