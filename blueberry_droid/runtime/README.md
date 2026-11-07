# Blueberry Android 0.1.0

This experimental package builds ReDroid Android 13 with a bootless Magisk
runtime for BlueberryQDB. Android account sessions and Magisk data live in a
persistent Android data volume. The upstream ReDroid multi-architecture image
is pinned by digest. The bootless Magisk 30.7 APK is pinned by SHA-256 and its
architecture is selected explicitly during the build.

The runtime is the ayasa520 Magisk fork, not an upstream Magisk release.
Upstream Magisk 31.1 does not have the bootless setup flags this container
integration needs. No Play Integrity spoofing modules, device fingerprints,
Google credentials, MyQ credentials or integrity verdicts are bundled. Root
and Magisk installation do not establish that MyQ will issue App Check.

## Host deployment

The direct container needs Linux Android Binder support and a privileged
container runtime. Rootless Podman is not supported for this Android init.
From this directory, build with `python manage.py build --rootful`, then start
with `python manage.py start --rootful`. The host port is bound only to
127.0.0.1:6555 by default. The container is named blueberry-android and its
named data volume is blueberry-android-data. The launcher refuses to replace
an existing container. It never deletes its data volume.

Use `adb connect 127.0.0.1:6555` and `scrcpy -s 127.0.0.1:6555` for setup.
Install your authorized MyQ application in Android. Google Play services are installed automatically from the pinned Android 13
MindTheGapps packages. APK installation and integrity verdicts are separate
from signing in to Google or MyQ. Existing physical phone
sessions are not copied or changed.

Fedora with host SELinux cannot use the direct container in this release.
The startup guard fails rather than modifying host security policy. Local
upstream ReDroid probes failed while initializing Android properties on
Fedora 44 with SELinux enforcing. Leave SELinux enabled and use the VM path.

## Isolated VM path

The VM wrapper keeps Android and Binder inside an Ubuntu 24.04 guest. It
requires x86-64 KVM, about 6 GiB guest RAM, and a 24 GiB sparse guest disk.
It downloads the current official Ubuntu cloud image and verifies the SHA-256
published by Ubuntu over HTTPS. The downloaded image and checksum metadata
are retained in /data, so restarts use the same guest. This is not a pinned
Ubuntu build across fresh installations.

Build from the repository root with
`sudo podman build -f sre/BlueberryAndroid/vm/Dockerfile -t localhost/blueberry-android-vm:0.1.0 sre/BlueberryAndroid`.
Start with
`sudo podman run -d --name blueberry-android-vm --device /dev/kvm --security-opt label=disable --network host --memory 8g --pids-limit 1024 -v blueberry-android-vm-data:/data localhost/blueberry-android-vm:0.1.0`.
The SELinux label exception applies to this KVM runner container only.

First boot installs the Binder kernel modules in the guest and records the
generated SSH key. The guest no longer builds or runs Android itself: the
Android runtime is deployed into the guest by BlueberryDroid over SSH with
rootful Podman, and that single instance is the one the stack targets.
Progress is in /data/console.log inside the runner. The guest has no password login. QEMU binds its forwarded ports to server
loopback, while the VM runner uses host networking to avoid nested NAT. Its generated SSH key stays in
/data/ssh. The runner includes an SSH client for maintenance through its
internal port 2222, using the blueberry account and that key. No SSH host
port is exposed by the example command.

## Home Assistant controller

This directory is the remote runtime payload. It is not a Home Assistant
app. Install BlueberryDroid for ingress and SSH deployment, and BlueberryQDB
for the ADB API. BlueberryDroid uploads these sources and uses rootful Podman
on the remote server. The Android data volume remains on that server.

The image configures Zygisk and performs one Android restart on first boot.
The marker and setting are stored in /data, so subsequent boots retain them.
It uses a root ADB configuration for this dedicated ReDroid instance. Keep
ADB on server loopback behind the controller's SSH tunnel.

BlueberryQDB provides runtime metadata, screenshot and input APIs, App Check
extraction, and pinned module installation. Screenshot and input operations
require a network ReDroid target and refuse connected physical phones.

## Sources

ReDroid documentation https://github.com/remote-android/redroid-doc

Bootless integration reference https://github.com/ayasa520/redroid-script

Bootless Magisk source and release https://github.com/ayasa520/Magisk/releases/tag/v30.7

Upstream Magisk https://github.com/topjohnwu/Magisk

Ubuntu images https://cloud-images.ubuntu.com/noble/

Magisk is distributed under its upstream GPL license. The image obtains its
APK directly from the linked release. Source provenance is recorded here;
no APK or account secrets are committed to this repository.
