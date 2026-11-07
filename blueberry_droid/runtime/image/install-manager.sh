#!/system/bin/sh
# An authenticated ADB shell is the root BlueberryQDB falls back to when adbd
# is not privileged. Grant it first so no later failure can leave the instance
# without usable root; Magisk itself is already running at this point.
/sbin/magisk --sqlite "INSERT OR REPLACE INTO policies (uid,policy,until,logging,notification) VALUES (2000,2,0,0,0);"

core=/data/adb/magisk
source=/system/etc/blueberry-magisk
mkdir -p "$core"
# Staging is retried on the next boot. It must not abort the script: the
# manager install and the Zygisk switch below are independent of it, and a
# partial copy is detected by the marker instead of by an early exit.
if [ ! -f /data/adb/blueberry-core-30.7 ] || [ ! -f "$core/util_functions.sh" ]; then
    for name in busybox init-ld magisk magiskboot magiskinit magiskpolicy; do
        if cp "$source/$name" "$core/$name"; then chmod 0755 "$core/$name"; fi
    done
    for name in addon.d.sh app_functions.sh boot_patch.sh module_installer.sh uninstaller.sh util_functions.sh stub.apk; do
        if cp "$source/$name" "$core/$name"; then chmod 0644 "$core/$name"; fi
    done
    if [ -f "$core/busybox" ] && [ -f "$core/magisk" ] && [ -f "$core/util_functions.sh" ]; then
        touch /data/adb/blueberry-core-30.7
    fi
fi

marker=/data/adb/blueberry-manager-30.7
if [ ! -f "$marker" ]; then
    result=$(pm install -r /system/etc/blueberry-magisk/manager.apk 2>&1)
    case "$result" in
        *Success*) touch "$marker" ;;
    esac
fi

# Configure Zygisk before handing the Android instance to QDB. Restart once
# so the first zygote uses the saved setting. Container policy preserves data.
zygisk_marker=/data/adb/blueberry-zygisk-enabled
if [ ! -f "$zygisk_marker" ]; then
    if /sbin/magisk --sqlite "INSERT OR REPLACE INTO settings (key,value) VALUES ('zygisk',1);"; then
        touch "$zygisk_marker"
        setprop sys.powerctl reboot
    fi
fi

exit 0
