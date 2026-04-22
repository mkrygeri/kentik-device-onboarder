#!/usr/bin/env bash
# Build .deb and .rpm packages for kentik-device-onboarder using fpm.
#
# Prerequisites:
#   gem install fpm        (requires Ruby)
#   apt install rpm        (on Debian/Ubuntu, to build RPMs)
#
# Usage:
#   ./packaging/build-packages.sh           # build both deb and rpm
#   ./packaging/build-packages.sh deb       # build only deb
#   ./packaging/build-packages.sh rpm       # build only rpm

set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PACKAGING_DIR="$REPO_ROOT/packaging"
DIST_DIR="$REPO_ROOT/dist"
VERSION=$(cat "$REPO_ROOT/VERSION")
BUILD_TARGET="${1:-all}"

if ! command -v fpm >/dev/null 2>&1; then
    echo "fpm is not installed. Install it with: gem install fpm" >&2
    exit 1
fi

echo "Building kentik-device-onboarder version $VERSION"

# ─── Staging area ────────────────────────────────────────────────────────────
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

install -d "$STAGING/opt/kentik-device-onboarder"
install -d "$STAGING/etc/kentik-device-onboarder"
install -d "$STAGING/usr/lib/systemd/system"

install -m 0755 "$REPO_ROOT/kentik_device_onboarder.py" \
    "$STAGING/opt/kentik-device-onboarder/kentik_device_onboarder.py"

install -m 0644 "$REPO_ROOT/kentik-device-onboarder.service" \
    "$STAGING/usr/lib/systemd/system/kentik-device-onboarder.service"

install -m 0644 "$REPO_ROOT/kentik-device-onboarder.env.example" \
    "$STAGING/etc/kentik-device-onboarder/onboarder.env.example"

# ─── Common fpm arguments ─────────────────────────────────────────────────────
FPM_COMMON=(
    -s dir
    -n kentik-device-onboarder
    -v "$VERSION"
    --iteration 1
    --description "Automatically onboards unregistered Kentik flow-pak devices into the Kentik platform"
    --url "https://github.com/kentik/kentik-device-onboarder"
    --license "Apache-2.0"
    --vendor "Kentik Technologies, Inc."
    --maintainer "Kentik Technologies, Inc. <support@kentik.com>"
    --depends python3
    --config-files /etc/kentik-device-onboarder/onboarder.env.example
    --after-install  "$PACKAGING_DIR/postinst"
    --before-remove  "$PACKAGING_DIR/prerm"
    -C "$STAGING"
)

mkdir -p "$DIST_DIR"

# ─── DEB ─────────────────────────────────────────────────────────────────────
build_deb() {
    local output="$DIST_DIR/kentik-device-onboarder_${VERSION}_all.deb"
    echo "Building DEB: $output"
    fpm "${FPM_COMMON[@]}" \
        -t deb \
        -a all \
        --deb-no-default-config-files \
        --deb-systemd "$REPO_ROOT/kentik-device-onboarder.service" \
        -p "$output" \
        opt/kentik-device-onboarder \
        usr/lib/systemd/system \
        etc/kentik-device-onboarder
    echo "DEB built: $output"
}

# ─── RPM ─────────────────────────────────────────────────────────────────────
build_rpm() {
    local output="$DIST_DIR/kentik-device-onboarder-${VERSION}-1.noarch.rpm"
    echo "Building RPM: $output"
    fpm "${FPM_COMMON[@]}" \
        -t rpm \
        -a noarch \
        --rpm-summary "Kentik device onboarder" \
        --rpm-dist "el8" \
        -p "$output" \
        opt/kentik-device-onboarder \
        usr/lib/systemd/system \
        etc/kentik-device-onboarder
    echo "RPM built: $output"
}

# ─── Dispatch ─────────────────────────────────────────────────────────────────
case "$BUILD_TARGET" in
    deb)  build_deb ;;
    rpm)  build_rpm ;;
    all)  build_deb; build_rpm ;;
    *)
        echo "Unknown target: $BUILD_TARGET  (expected: deb | rpm | all)" >&2
        exit 1
        ;;
esac

echo "Done. Packages written to $DIST_DIR/"
