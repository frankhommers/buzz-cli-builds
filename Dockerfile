# RUST_IMAGE is supplied from the validated, digest-pinned upstream.json.
# No --platform override here: the caller validates the native Docker daemon.
ARG RUST_IMAGE=rust:1.95.0-alpine@sha256:606fd313a0f49743ee2a7bd49a0914bab7deedb12791f3a846a34a4711db7ed2
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
