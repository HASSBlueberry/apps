ARG ANDROID_IMAGE=docker.io/redroid/redroid@sha256:5a42a569ee1d7c71796c0385e906cbaa4c3e0a162a56d9f26b29bdb1befac13b
FROM docker.io/library/python:3.13-slim-bookworm AS runtime
ARG BUILD_ARCH=amd64
ARG INSTALL_GAPPS=1
COPY image/gapps.py /gapps.py
RUN if [ "$INSTALL_GAPPS" = 1 ]; then python /gapps.py "$BUILD_ARCH" /rootfs; fi
COPY image/prepare.py /prepare.py
RUN python /prepare.py "$BUILD_ARCH" /rootfs
COPY image/install-manager.sh /rootfs/system/etc/blueberry-magisk/install-manager.sh
COPY image/blueberry-magisk.rc /rootfs/system/etc/init/blueberry-magisk.rc
RUN chmod 0644 /rootfs/system/etc/init/blueberry-magisk.rc /rootfs/system/etc/blueberry-magisk/install-manager.sh
FROM docker.io/library/alpine:3.22 AS guard
RUN apk add --no-cache build-base
COPY image/guard.c /guard.c
RUN cc -static -Os -Wall -Wextra -Werror /guard.c -o /blueberry-init
FROM ${ANDROID_IMAGE}
COPY --from=runtime /rootfs/ /
COPY --from=guard /blueberry-init /blueberry-init
ENTRYPOINT ["/blueberry-init", "qemu=1", "androidboot.hardware=redroid", "ro.secure=0", "ro.setupwizard.mode=DISABLED", "androidboot.use_memfd=1", "androidboot.redroid_gpu_mode=guest", "androidboot.redroid_width=720", "androidboot.redroid_height=1280"]
