"""Extract only the pinned bootless Magisk runtime for the target architecture."""
import hashlib
import io
from pathlib import Path
import sys
import urllib.request
import zipfile

URL='https://github.com/ayasa520/Magisk/releases/download/v30.7/Magisk-v30.7.apk'
SHA256='a1292054b25c31f19f4cdc3f33b8b48020574fc237eabd7633e54193698ac396'


def prepare(architecture, destination, apk=None):
    abi={'amd64':'x86_64','aarch64':'arm64-v8a'}[architecture]
    if apk is None:
        with urllib.request.urlopen(URL,timeout=60) as response:
            apk=response.read(32*1024*1024+1)
    if hashlib.sha256(apk).hexdigest()!=SHA256:raise ValueError('Magisk APK checksum mismatch')
    target=Path(destination)/'system/etc/blueberry-magisk'
    target.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(apk)) as archive:
        for binary in ('busybox','init-ld','magisk','magiskboot','magiskinit','magiskpolicy'):
            content=archive.read(f'lib/{abi}/lib{binary}.so')
            if binary=='magisk' and b'--setup-sbin' not in content:raise ValueError('Bootless runtime is required')
            path=target/binary
            path.write_bytes(content);path.chmod(0o755)
        for name in ('addon.d.sh','app_functions.sh','boot_patch.sh','module_installer.sh','uninstaller.sh','util_functions.sh'):
            path=target/name;path.write_bytes(archive.read('assets/'+name));path.chmod(0o644)
        (target/'stub.apk').write_bytes(archive.read('assets/stub.apk'))
    (target/'manager.apk').write_bytes(apk)
    (Path(destination)/'sbin').mkdir(exist_ok=True)


if __name__=='__main__':
    prepare(sys.argv[1],sys.argv[2])
