#!/usr/bin/env bash
# Build audio-spider .deb inside a disposable Ubuntu 22.04 container so the
# host system doesn't need debhelper/dh-python/pybuild installed.
# Output lands in ./dist/.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_TAG="audio-spider-builder:22.04"
OUT_DIR="${REPO_ROOT}/dist"

mkdir -p "${OUT_DIR}"

echo "==> Building builder image (${IMAGE_TAG})"
docker build -t "${IMAGE_TAG}" "${REPO_ROOT}/packaging"

echo "==> Running build"
docker run --rm \
    -e HOST_UID="$(id -u)" \
    -e HOST_GID="$(id -g)" \
    -v "${REPO_ROOT}:/src:ro" \
    -v "${OUT_DIR}:/out" \
    "${IMAGE_TAG}"

echo
echo "==> Generating SHA-256 checksums"
( cd "${OUT_DIR}" && for f in *.deb; do
    [ -e "${f}" ] || { echo "no .deb produced"; exit 1; }
    sha256sum "${f}" > "${f}.sha256"
    echo "  ${f}.sha256"
done )

echo
echo "==> Artifacts in ${OUT_DIR}:"
ls -1 "${OUT_DIR}"/*.deb "${OUT_DIR}"/*.sha256 2>/dev/null
