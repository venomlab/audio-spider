#!/usr/bin/env bash
# Runs inside the build container. Copies the read-only source bind-mount
# into a writable build dir (dpkg-buildpackage drops artifacts in the parent
# directory, so we want full control over that parent), builds the .deb,
# and ships every artifact into the /out bind-mount with the caller's UID.
set -euo pipefail

: "${HOST_UID:?HOST_UID env var required}"
: "${HOST_GID:?HOST_GID env var required}"

SRC=/src
WORK=/build/audio-spider
OUT=/out

mkdir -p "${WORK}"
# rsync would be cleaner but pulls in an extra dep; tar pipe is in coreutils.
( cd "${SRC}" && tar --exclude='./.venv' \
                     --exclude='./.git' \
                     --exclude='./dist' \
                     --exclude='./__pycache__' \
                     --exclude='./.mypy_cache' \
                     --exclude='./.ruff_cache' \
                     --exclude='./.pytest_cache' \
                     -cf - . ) | tar -C "${WORK}" -xf -

cd "${WORK}"
dpkg-buildpackage -us -uc -b

# dpkg-buildpackage writes ../*.deb relative to source dir
cp -v /build/*.deb /build/*.buildinfo /build/*.changes "${OUT}/" || true

# Optional sanity check; lintian failures don't abort the build
lintian --suppress-tags new-package-should-close-itp-bug,no-manual-page \
        /build/*.deb || true

chown -R "${HOST_UID}:${HOST_GID}" "${OUT}"
