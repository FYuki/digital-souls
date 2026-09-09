#!/bin/sh
set -eu
rustc --version
rustc --edition=2021 --test retry_policy_test.rs -o target-retry-policy-test
./target-retry-policy-test
cargo build --locked --release --package livekit-ffi
sha256sum target/release/liblivekit_ffi.so
ldd target/release/liblivekit_ffi.so
