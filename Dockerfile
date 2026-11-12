# RUST_IMAGE is supplied from the validated, digest-pinned upstream.json.
# No --platform override here: the caller validates the native Docker daemon.
ARG RUST_IMAGE=rust:1.98.1-alpine@sha256:7cc1c22d77d9432f7fe012a70e6d3e555af54c2a6832700ed7d553f1769ae89f
FROM ${RUST_IMAGE} AS builder
ARG BUZZ_TARGET
ENV BUILD_CONTAINER=1
RUN apk add --no-cache python3 build-base cmake perl pkgconf git ca-certificates file binutils
WORKDIR /recipe
COPY upstream.json ./upstream.json
COPY scripts/build.py ./scripts/build.py
COPY license-supplements/ ./license-supplements/
# Resolve/compile only buzz-cli, preserving the complete upstream Cargo.lock.
RUN python3 scripts/build.py --target "${BUZZ_TARGET}" --container-phase prepare
# The real library suite runs after preparation with the container network off.
RUN --network=none python3 scripts/build.py --target "${BUZZ_TARGET}" --container-phase test
RUN --network=none python3 scripts/build.py --target "${BUZZ_TARGET}" --container-phase build
FROM scratch AS artifact
COPY --from=builder /recipe/artifact/ /
