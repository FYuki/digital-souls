FROM rust:1.97.1-bookworm@sha256:408fe88047cef61a2087653b0c5255fa51c0f2d6d94ddedd7a2562a9b91a46f6
RUN apt-get update && apt-get install -y --no-install-recommends clang lld cmake ninja-build protobuf-compiler pkg-config libglib2.0-dev libssl-dev libx11-dev libxext-dev libxdamage-dev libxrandr-dev libxfixes-dev libxcomposite-dev libdrm-dev libgbm-dev libva-dev && rm -rf /var/lib/apt/lists/*
ENV CARGO_BUILD_JOBS=4 CC=clang CXX=clang++
WORKDIR /work
