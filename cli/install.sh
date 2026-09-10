#!/bin/sh
# Installs the canyonos CLI from a prebuilt binary attached to the latest
# "cli-v*" GitHub release. Usage: curl -fsSL <raw-url>/install.sh | sh

set -eu

REPO="CanyonCodeCoreAI/canyoncodecore"
INSTALL_DIR="${CANYONOS_INSTALL_DIR:-$HOME/.local/bin}"

os="$(uname -s)"
arch="$(uname -m)"

case "$os" in
    Darwin)
        case "$arch" in
            arm64) asset="canyonos-macos-arm64" ;;
            x86_64) asset="canyonos-macos-x86_64" ;;
            *) echo "error: unsupported macOS arch: $arch" >&2; exit 1 ;;
        esac
        ;;
    Linux)
        case "$arch" in
            x86_64) asset="canyonos-linux-x86_64" ;;
            aarch64|arm64) asset="canyonos-linux-arm64" ;;
            *) echo "error: unsupported Linux arch: $arch" >&2; exit 1 ;;
        esac
        ;;
    *)
        echo "error: unsupported OS: $os (try 'pip install canyonos' instead)" >&2
        exit 1
        ;;
esac

tag="${CANYONOS_VERSION:-}"
if [ -z "$tag" ]; then
    tag="$(curl -fsSL "https://api.github.com/repos/$REPO/releases" \
        | grep '"tag_name"' \
        | grep -o 'cli-v[0-9][^"]*' \
        | head -n1)"
fi

if [ -z "$tag" ]; then
    echo "error: could not find a cli-v* release for $REPO" >&2
    exit 1
fi

url="https://github.com/$REPO/releases/download/$tag/$asset"

echo "Installing canyonos $tag ($asset) to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
curl -fsSL "$url" -o "$INSTALL_DIR/canyonos"
chmod +x "$INSTALL_DIR/canyonos"

echo "Installed: $INSTALL_DIR/canyonos"
case ":$PATH:" in
    *":$INSTALL_DIR:"*) ;;
    *) echo "Add $INSTALL_DIR to your PATH to use 'canyonos' directly." ;;
esac
