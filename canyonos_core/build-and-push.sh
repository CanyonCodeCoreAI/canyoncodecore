#!/bin/sh
# Builds and pushes saakeths/canyonos:latest for linux/amd64 and linux/arm64.
# NOTE: Will need to create an enterprise account on a registry and switch from saakeths to canyonos, this is temporary

set -eu

IMAGE="saakeths/canyonos:latest"
PLATFORMS="linux/amd64,linux/arm64"
BUILDER_NAME="canyonos-multiarch"

if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
    docker buildx create --name "$BUILDER_NAME" --driver docker-container --use
else
    docker buildx use "$BUILDER_NAME"
fi

docker buildx build \
    --builder "$BUILDER_NAME" \
    --platform "$PLATFORMS" \
    -f canyonos_core/Dockerfile \
    -t "$IMAGE" \
    --push \
    .
