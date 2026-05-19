# Build host for the audio-spider .deb package.
# Ubuntu 22.04 is used as the lowest supported target — the resulting
# Architecture: all .deb installs cleanly on 22.04 and 24.04.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LC_ALL=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
        devscripts \
        debhelper \
        dh-python \
        fakeroot \
        lintian \
        pybuild-plugin-pyproject \
        python3-all \
        python3-hatchling \
        python3-setuptools \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY build-in-container.sh /usr/local/bin/build-in-container.sh
RUN chmod +x /usr/local/bin/build-in-container.sh

ENTRYPOINT ["/usr/local/bin/build-in-container.sh"]
