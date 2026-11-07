#!/bin/sh
set -eu
if [ "$(id -u)" != 0 ]; then exec sudo -n sh "$0"; fi
cd .local/share/blueberry-droid
name=$(cat runtime-container)
case "$name" in blueberry-android|blueberry-android-vm) ;; *) exit 1 ;; esac
key=$(cat adb-key.pub)
install_key='set -eu; mkdir -p /data/misc/adb; touch /data/misc/adb/adb_keys; key=$(cat /data/blueberry-adb-key.pub); grep -Fqx "$key" /data/misc/adb/adb_keys || printf "%s\n" "$key" >> /data/misc/adb/adb_keys; chown 1000:2000 /data/misc/adb /data/misc/adb/adb_keys; chmod 2750 /data/misc/adb; chmod 0640 /data/misc/adb/adb_keys; rm -f /data/blueberry-adb-key.pub; setprop ctl.restart adbd'
if [ "$name" = blueberry-android ]; then
    podman cp adb-key.pub "$name:/data/blueberry-adb-key.pub"
    for attempt in $(seq 1 60); do
        if podman exec "$name" /system/bin/sh -c "$install_key"; then exit 0; fi
        sleep 2
    done
else
    # This loopback guest is created and owned by this deployment.
    for attempt in $(seq 1 240); do
        if printf '%s\n' "$key" | podman exec -i "$name" ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/data/known_hosts -i /data/ssh -p 2222 blueberry@127.0.0.1 'sudo docker exec -i blueberry-android sh -c "cat > /data/blueberry-adb-key.pub"'; then
            if printf '%s\n' "$install_key" | podman exec -i "$name" ssh -o BatchMode=yes -o ConnectTimeout=5 -o UserKnownHostsFile=/data/known_hosts -i /data/ssh -p 2222 blueberry@127.0.0.1 'sudo docker exec -i blueberry-android /system/bin/sh'; then exit 0; fi
        fi
        sleep 5
    done
fi
exit 1
