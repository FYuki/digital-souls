FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS sdk-source
RUN apt-get update && apt-get install -y --no-install-recommends patch && rm -rf /var/lib/apt/lists/*
COPY scripts/voice_quality/native_sdk_experiment/prepare.py scripts/voice_quality/native_sdk_experiment/short-outage-retry.patch /sdk-tools/
RUN python /sdk-tools/prepare.py /sdk

FROM rust:1.97.1-bookworm@sha256:408fe88047cef61a2087653b0c5255fa51c0f2d6d94ddedd7a2562a9b91a46f6 AS sdk-build
RUN apt-get update && apt-get install -y --no-install-recommends clang lld cmake ninja-build protobuf-compiler pkg-config python3 libglib2.0-dev libssl-dev libx11-dev libxext-dev libxdamage-dev libxrandr-dev libxfixes-dev libxcomposite-dev libdrm-dev libgbm-dev libva-dev && rm -rf /var/lib/apt/lists/*
ENV CARGO_BUILD_JOBS=4 CC=clang CXX=clang++ RUSTUP_TOOLCHAIN=1.97.1
WORKDIR /work
COPY --from=sdk-source /sdk/source /work
COPY --from=sdk-source /sdk-tools /sdk-tools
COPY scripts/voice_quality/native_sdk_experiment/build.sh scripts/voice_quality/native_sdk_experiment/package.py /sdk-tools/
RUN sh /sdk-tools/build.sh
RUN python3 /sdk-tools/package.py /work /sdk-tools/short-outage-retry.patch /native-livekit

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

WORKDIR /app
COPY backend/requirements.txt /tmp/backend-requirements.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/backend-requirements.txt
COPY --from=sdk-build /native-livekit /opt/digital-souls/livekit
ENV LIVEKIT_LIB_PATH=/opt/digital-souls/livekit/liblivekit_ffi.so
RUN python -c 'import json; from importlib.metadata import version; from livekit.rtc._ffi_client import FfiClient; record=json.load(open("/opt/digital-souls/livekit/build.json")); assert version("livekit") == record["python_sdk_version"]; assert FfiClient.instance is not None'
COPY backend /app/backend
COPY characters /app/characters
COPY contracts /app/contracts

ENTRYPOINT ["/app/backend/container-entrypoint.sh"]
