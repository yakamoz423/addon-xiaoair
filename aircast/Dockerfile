ARG BUILD_FROM=ghcr.io/hassio-addons/debian-base:9.1.0
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

# Set shell
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Copy root filesystem
COPY rootfs /

# Setup base — use packaged shairport-sync (includes pipe/avahi/soxr).
# Compiling from source made every HA update take a long time.
ARG BUILD_ARCH=amd64
ARG AIRCAST_VERSION="1.9.3"
RUN \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        unzip \
        curl \
        ca-certificates \
        python3 \
        python3-requests \
        ffmpeg \
        shairport-sync \
        libnss-mdns \
        libavahi-client3 \
        avahi-daemon \
        avahi-utils \
        dbus \
        libasound2-plugins \
        xxd \
    \
    && curl -fsSL -o /tmp/airconnect.zip \
      "https://github.com/philippe44/AirConnect/releases/download/${AIRCAST_VERSION}/AirConnect-${AIRCAST_VERSION}.zip" \
    && unzip -d /tmp /tmp/airconnect.zip \
    \
    && if [ "${BUILD_ARCH}" = "aarch64" ]; then AIRCONNECT_ARCH="aarch64"; \
    elif [ "${BUILD_ARCH}" = "amd64" ]; then AIRCONNECT_ARCH="x86_64"; \
    else echo "Unsupported arch: ${BUILD_ARCH}" >&2; exit 1; fi \
    \
    && cp "/tmp/aircast-linux-${AIRCONNECT_ARCH}-static" /usr/bin/aircast \
    && chmod a+x /usr/bin/aircast \
    \
    && chmod a+x /usr/bin/media-players.py \
    && chmod a+x /usr/bin/esphome-discovery.py \
    && chmod a+x /usr/bin/esphome-airplay-bridge.py \
    && chmod a+x /usr/bin/shairport-manager.py \
    && chmod a+x /usr/bin/shairport-play-handler.py \
    && chmod a+x /usr/bin/xiaoair-ui.py \
    && chmod a+x /etc/s6-overlay/s6-rc.d/xiaoair-ui/run \
    && chmod a+x /etc/s6-overlay/s6-rc.d/xiaoair-ui/finish \
    \
    && shairport-sync -V \
    \
    && apt-get purge -y --auto-remove unzip \
    && rm -fr \
        /tmp/* \
        /var/{cache,log}/* \
        /var/lib/apt/lists/*

# Build arguments
ARG BUILD_ARCH
ARG BUILD_DATE
ARG BUILD_DESCRIPTION
ARG BUILD_NAME
ARG BUILD_REF
ARG BUILD_REPOSITORY
ARG BUILD_VERSION

# Labels
LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="yakamoz423" \
    org.opencontainers.image.title="${BUILD_NAME}" \
    org.opencontainers.image.description="${BUILD_DESCRIPTION}" \
    org.opencontainers.image.vendor="XiaoAir" \
    org.opencontainers.image.authors="yakamoz423" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.url="https://github.com/yakamoz423/addon-xiaoair" \
    org.opencontainers.image.source="https://github.com/${BUILD_REPOSITORY}" \
    org.opencontainers.image.documentation="https://github.com/${BUILD_REPOSITORY}/blob/main/README.md" \
    org.opencontainers.image.created=${BUILD_DATE} \
    org.opencontainers.image.revision=${BUILD_REF} \
    org.opencontainers.image.version=${BUILD_VERSION}
