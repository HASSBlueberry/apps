#!/bin/sh
set -eu
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
if [ "$(id -u)" != 0 ]; then exec sudo -n sh "$0" "$@"; fi
exec 9>/var/lock/blueberry-droid.lock
flock -n 9 || { echo deployment_already_running >&2; exit 51; }
deployment_origin=$PWD
cd .local/share/blueberry-droid/runtime
if [ "$(id -u)" = 0 ]; then
    elevate=''
else
    sudo -n true || { echo privilege_required >&2; exit 41; }
    elevate='sudo -n'
fi
if ! command -v podman >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        $elevate apt-get update
        $elevate apt-get install -y podman
    elif command -v dnf >/dev/null 2>&1; then
        $elevate dnf install -y podman
    else
        echo unsupported_package_manager >&2; exit 42
    fi
fi
$elevate systemctl enable podman-restart.service
refresh_existing() {
    container_name=$1
    desired_image=$2
    data_volume=$3
    if ! $elevate podman container exists "$container_name"; then return 1; fi
    actual=$($elevate podman inspect --format '{{.Config.Image}}' "$container_name")
    actual_volume=$($elevate podman inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$container_name")
    [ "$actual" = "$desired_image" ] && [ "$actual_volume" = "$data_volume" ] || { echo container_name_in_use >&2; exit 46; }
    old_id=$($elevate podman inspect --format '{{.Image}}' "$container_name")
    new_id=$($elevate podman image inspect --format '{{.Id}}' "$desired_image")
    if [ "${old_id#sha256:}" = "${new_id#sha256:}" ]; then
        if [ "$($elevate podman inspect --format '{{.State.Running}}' "$container_name")" = true ]; then
            $elevate podman stop -t 30 "$container_name"
        fi
        return 0
    fi
    # Replace only this product's image and named data volume. Keep the data.
    if [ "$($elevate podman inspect --format '{{.State.Running}}' "$container_name")" = true ]; then
        $elevate podman stop -t 30 "$container_name"
    fi
    $elevate podman rm "$container_name"
    return 1
}
mode=${1:-auto}
case "$mode" in auto|direct|vm) ;; *) exit 43 ;; esac
selinux=disabled
if command -v getenforce >/dev/null 2>&1; then selinux=$(getenforce); fi
if [ "$mode" = auto ]; then
    case "$selinux" in Enforcing|Permissive) mode=vm ;; *) mode=direct ;; esac
fi
if [ "$mode" = vm ]; then
    [ -c /dev/kvm ] || { echo kvm_required >&2; exit 44; }
    [ "$(uname -m)" = x86_64 ] || { echo vm_arch_unsupported >&2; exit 45; }
    image=localhost/blueberry-android-vm:0.1.0
    name=blueberry-android-vm
    $elevate podman build --network host -f vm/Dockerfile -t "$image" .
    if ! refresh_existing "$name" "$image" blueberry-android-vm-data; then
        $elevate podman create --restart unless-stopped --name "$name" --device /dev/kvm --security-opt label=disable --network host --memory 8g --pids-limit 4096 -v blueberry-android-vm-data:/data "$image"
    fi
else
    case "$selinux" in Enforcing|Permissive) echo use_isolated_vm >&2; exit 47 ;; esac
    if ! grep -q binder /proc/filesystems; then
        $elevate modprobe binder_linux devices=binder,hwbinder,vndbinder || { echo binder_required >&2; exit 48; }
    fi
    case "$(uname -m)" in x86_64) arch=amd64 ;; aarch64) arch=aarch64 ;; *) exit 45 ;; esac
    # Some minimal kernels do not autoload Netfilter extensions for Netavark.
    for module in bridge br_netfilter nf_conntrack nf_nat nft_compat xt_comment xt_mark xt_MASQUERADE xt_conntrack xt_addrtype iptable_filter iptable_nat; do
        $elevate modprobe "$module" || { echo network_kernel_module_required >&2; exit 50; }
    done
    if ! $elevate podman network exists blueberry-droid; then
        # --interface-name needs podman >= 4.5; older releases get the default
        # interface and ownership is proven by the label below instead.
        if $elevate podman network create --help 2>&1 | grep -q -- '--interface-name'; then
            $elevate podman network create --interface-name bbdroid0 --label io.blueberry.managed=droid blueberry-droid
        else
            $elevate podman network create --label io.blueberry.managed=droid blueberry-droid
        fi
    fi
    if ! $elevate podman network inspect blueberry-droid 2>/dev/null | grep -q 'io.blueberry.managed.*droid'; then
        echo network_name_in_use >&2; exit 49
    fi
    $elevate mkdir -p /usr/local/lib/blueberry-droid
    $elevate tee /usr/local/lib/blueberry-droid/network.sh >/dev/null <<'POLICY'
#!/bin/sh
set -eu
# Docker can set FORWARD to DROP. Allow only the dedicated Android bridge.
if command -v iptables >/dev/null 2>&1 && iptables -nL DOCKER-USER >/dev/null 2>&1; then
    iptables -C DOCKER-USER -i bbdroid0 -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER 1 -i bbdroid0 -j ACCEPT
    iptables -C DOCKER-USER -o bbdroid0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER 1 -o bbdroid0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
fi
POLICY
    $elevate sh /usr/local/lib/blueberry-droid/network.sh
    $elevate tee /etc/systemd/system/blueberry-droid-network.service >/dev/null <<'SERVICE'
[Unit]
Description=Blueberry Android bridge rules when Docker is present
After=network-online.target docker.service
Before=podman-restart.service
[Service]
Type=oneshot
ExecStart=/bin/sh /usr/local/lib/blueberry-droid/network.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
SERVICE
    if [ -d /etc/modules-load.d ]; then
        printf '%s\n' binder_linux br_netfilter nft_compat xt_comment xt_mark xt_MASQUERADE xt_conntrack xt_addrtype iptable_filter iptable_nat | $elevate tee /etc/modules-load.d/blueberry-droid.conf >/dev/null
    fi
    $elevate chmod 0644 /etc/systemd/system/blueberry-droid-network.service /usr/local/lib/blueberry-droid/network.sh
    $elevate systemctl daemon-reload
    $elevate systemctl enable blueberry-droid-network.service podman-restart.service
    image=localhost/blueberry-android:0.1.0
    name=blueberry-android
    $elevate podman build --network host --build-arg "BUILD_ARCH=$arch" -t "$image" .
    if ! refresh_existing "$name" "$image" blueberry-android-data; then
        $elevate podman create --restart unless-stopped --name "$name" --privileged --network blueberry-droid --memory 4g --pids-limit 4096 -p 127.0.0.1:6555:5555 -v blueberry-android-data:/data "$image"
    fi
fi
# A dedicated unit starts the chosen runtime even on Podman versions whose
# global restart service handles only the "always" policy.
$elevate tee /etc/systemd/system/blueberry-droid-runtime.service >/dev/null <<SERVICE
[Unit]
Description=Blueberry Android runtime
Wants=network-online.target
After=network-online.target blueberry-droid-network.service
[Service]
Type=oneshot
Delegate=yes
RemainAfterExit=yes
ExecStart=/usr/bin/podman start $name
ExecStop=-/usr/bin/podman stop -t 30 $name
[Install]
WantedBy=multi-user.target
SERVICE
$elevate chmod 0644 /etc/systemd/system/blueberry-droid-runtime.service
$elevate systemctl daemon-reload
$elevate systemctl enable blueberry-droid-runtime.service

$elevate systemctl restart blueberry-droid-runtime.service
printf '%s\n' "$name" > ../runtime-container
cd "$deployment_origin"
sh .local/share/blueberry-droid/authorize-adb.sh
