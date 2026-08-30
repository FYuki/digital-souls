FROM node:22.22.0-bookworm-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94 AS build

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json /app/frontend/
RUN npm --prefix /app/frontend ci
COPY frontend /app/frontend
COPY contracts /app/contracts
RUN npm --prefix /app/frontend run build

FROM node:22.22.0-bookworm-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94
ENV NODE_ENV=production
WORKDIR /app
COPY --from=build /app/frontend /app/frontend
COPY --from=build /app/contracts /app/contracts
# Vite 5 はdev起動時にconfig変換用の一時fileとoptimizer cacheを作成する。
# 実行UID/GIDはhostに合わせるため、固定所有者ではなくsticky directoryで共有する。
RUN mkdir -p /app/frontend/node_modules/.vite \
    && chmod 1777 /app/frontend /app/frontend/node_modules/.vite
ENTRYPOINT ["/app/frontend/container-entrypoint.sh"]
