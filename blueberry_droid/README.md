# BlueberryDroid

BlueberryDroid is the Home Assistant ingress and SSH deployment app.
ReDroid runs on a separate Linux server, with a persistent Android volume.
BlueberryQDB owns the ADB API and App Check extraction. Neither Android nor
Podman runs on the Home Assistant host. The controller needs no protection
mode change or host kernel access.

Install BlueberryDroid and BlueberryQDB 0.5.0. In the Droid panel, configure
server hostname, SSH port, username, host key fingerprint, QDB URL and QDB key.
A generated public key is shown for installation in the server's
.ssh/authorized_keys. Alternatively use an SSH password or upload an
unencrypted private key. Public keys alone cannot sign SSH authentication.
Compare the observed server fingerprint with the key on that server before
saving it. Every connection enforces the saved fingerprint.

The server must use systemd. Its SSH account must be root or have
passwordless sudo for provisioning. Installation runs as a transient system
service and survives SSH interruption. Its job ID is saved in the controller
and tracking resumes after app restarts. Stop tracking leaves the server
service running; Deploy resumes a saved job.
The deployment script can install Podman through apt-get or dnf. It uploads
reviewed runtime sources through SFTP, builds the pinned image, and creates
only the named blueberry-android or blueberry-android-vm container. Existing
containers with an unexpected image are rejected. Android data is retained
in a named volume. Existing matching containers are started without deleting
them. The generated SSH public key can be authorized during deployment so
future connections can use it. Passwords and uploaded private keys are kept
in private app storage and are never returned to the dashboard.

Automatic mode uses a separate x86-64 KVM guest when SELinux is enabled on
the server. Direct mode requires host Android Binder support. VM mode needs
/dev/kvm, 6 GiB guest RAM, and a 24 GiB sparse guest disk. Both paths use
rootful Podman. Builds use server networking for their download stages.
The direct Android runtime uses a dedicated bridge. When Docker is present,
scoped forwarding rules allow outbound traffic for that bridge and established
replies, and a systemd unit reapplies those rules after restart. Required
Netfilter modules are loaded explicitly and recorded for subsequent boots. Host SELinux is never disabled, and the Magisk image refuses
to run directly in the host SELinux domain. The VM runner's label exception
applies only to the KVM container. A direct ARM64 Linux server is required
for the published ARM-only PlayIntegrityFork module.

The VM runner uses the server network namespace to avoid an extra NAT layer,
but QEMU binds its guest SSH and ADB listeners only to server loopback.
The remote ADB port is bound only to server loopback. BlueberryDroid forwards
it through authenticated SSH to its internal port 15555. Leave that port's
host mapping disabled. Set the panel's ADB tunnel address to the internal
BlueberryDroid app hostname followed by :15555. Deployment registers that
target with QDB. Clear QDB's pinned serial option first if it names another
phone. QDB retains the managed target across restarts. A completed deployment
reconnects its SSH tunnel automatically after controller restarts.

Use Start screen to view Android through QDB. Click to tap, use Back/Home,
and send text to the focused field. Screen bytes and typed text are not saved
by either app. This interface is restricted to a ReDroid network target, so
it does not silently operate a connected physical phone. It uses periodic
screenshots rather than a video streaming server.

The Android image enables Zygisk and restarts once on first boot.
The module action also verifies Zygisk configuration, installs SHA-256 pinned releases of
KOWX712 PlayIntegrityFix v4.9-inject-s and Shamiko 1.2.5-414, configures the MyQ and Google
Play services unstable processes in the denylist with enforcement disabled,
and requests an Android reboot. It first validates ReDroid, architecture,
Magisk and Google Play services. The selected package includes x86-64,
ARM64 and ARM libraries. The API checks the actual ZIP for the target ABI.
No hardware keyboxes are fetched or bundled. An installed module does not
establish a successful Play Integrity verdict or App Check token.

Google Play services are installed automatically into the Android image from
SHA-256 pinned, architecture-specific Android 13 MindTheGapps packages.
They may need Google account setup or updates through the forwarded UI.
The base MyQ application is installed separately, preserving its app data. The panel
accepts a compatible GMS-enabled ReDroid image pinned by digest. When supplied,
that image becomes the base for the Magisk overlay on the server. Unattended
module setup is enabled by default after Android boots; it reports a clear
prerequisite error if the architecture or Google services are missing.
Disable that checkbox to provision and inspect just the base runtime.
MyQ and a working Google services environment are required before App Check
issuance can be tested.
The image uses the SHA-256 pinned ayasa520 bootless Magisk release, whose
running daemon currently identifies as 30.6:MAGISK:R. Its APK release is
labelled v30.7. The panel reports the actual daemon version.

The local KVM guest has booted Android 13 with Magisk and Google services.
MyQ 5.323.0.1132341 was verified, installed through the ingress upload API,
and rendered through the forwarded screen. KOWX712 PlayIntegrityFix and
Shamiko were installed, their versions were read back after the requested
Android reboot, and Zygisk remained configured. A successful Google integrity
verdict and MyQ App Check issuance remain unverified.
The actual controller-to-server deployment flow has separate validation
coverage; credentials for the user's intended server are not configured yet.

Generate the Home Assistant build context with
`bash scripts/setup-ha-repositories.sh`, then build dist/apps/blueberry_droid.
Runtime sources live in sre/BlueberryAndroid. They are no longer published
as direct Home Assistant apps.

Sources

https://github.com/remote-android/redroid-doc

https://github.com/ayasa520/Magisk/releases/tag/v30.7

https://github.com/KOWX712/PlayIntegrityFix/releases/tag/v4.9-inject-s

https://github.com/LSPosed/LSPosed.github.io/releases/tag/shamiko-414

MyQ installation accepts a direct HTTPS APK or bundle URL, an uploaded APK,
APKS, APKM or XAPK file, or APKs read from one authorized USB phone. QDB uses
apksigner to verify the signature and Chamberlain certificate, then checks
the package name with aapt. Bundle paths are flattened, architecture splits
are selected for the target, and installation uses the existing data with -r.
Only application APK files are copied from a phone, never account data.
An already-installed MyQ app is retained during normal deployment. APKMirror
HTML pages are not APK payloads; download the signed file there or supply a
working direct file URL. No APKMirror account or undocumented API is required.

Android init file permissions are normalized in the image. A dedicated server
systemd runtime unit owns the container, so install-job cleanup and server
reboots do not stop it. This is separate from the transient install job.
The Google services source is an archived third-party MindTheGapps builder;
its exact downloaded bytes are pinned, and Google APK updates may still be
required. Installing integrity modules does not establish a successful
Google verdict or MyQ App Check issuance. The selected fork's own README
notes additional Android 13+ Device Integrity requirements.
