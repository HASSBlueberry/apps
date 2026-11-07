"""Prepare a persistent KVM guest that hosts the Blueberry Android runtime."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request

DATA=Path('/data')
ORIGIN='https://cloud-images.ubuntu.com/noble/current/'
NAME='noble-server-cloudimg-amd64.img'


def download_image():
    image=DATA/'ubuntu-base.img'
    if image.exists():return image
    with urllib.request.urlopen(ORIGIN+'SHA256SUMS',timeout=60) as response:
        lines=response.read(1024*1024).decode().splitlines()
    hashes=[line.split()[0] for line in lines if line.split()[-1].lstrip('*')==NAME]
    if len(hashes)!=1:raise RuntimeError('Cloud image checksum not found')
    expected=hashes[0]
    temporary=image.with_suffix('.download')
    digest=hashlib.sha256()
    with urllib.request.urlopen(ORIGIN+NAME,timeout=60) as response,temporary.open('wb') as output:
        while chunk:=response.read(1024*1024):
            output.write(chunk);digest.update(chunk)
    if digest.hexdigest()!=expected:raise RuntimeError('Cloud image checksum mismatch')
    temporary.replace(image)
    (DATA/'ubuntu-source.json').write_text(json.dumps({'source':ORIGIN+NAME,'sha256':expected}))
    return image


def main():
    if not Path('/dev/kvm').exists():raise RuntimeError('KVM is required for the isolated Android VM')
    DATA.mkdir(parents=True,exist_ok=True)
    key=DATA/'ssh'
    if not key.exists():subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True)
    base=download_image()
    disk=DATA/'android.qcow2'
    if not disk.exists():subprocess.run(['qemu-img','create','-f','qcow2','-F','qcow2','-b',str(base),str(disk),'24G'],check=True)
    seed=DATA/'seed.img'
    if not seed.exists():
        # The guest only prepares the kernel (binder) and SSH access; the
        # Android runtime itself is deployed by BlueberryDroid over SSH.
        user_data='''#cloud-config
users:
  - name: blueberry
    groups: [sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - PUBLIC_KEY
ssh_pwauth: false
package_update: true
packages: [ca-certificates]
runcmd:
  - [bash, -lc, 'set -eu; apt-get install -y linux-modules-extra-$(uname -r); modprobe binder_linux devices=binder,hwbinder,vndbinder; echo binder_linux > /etc/modules-load.d/blueberry-binder.conf; mkdir -p /opt/blueberry; touch /opt/blueberry/ready']
'''.replace('PUBLIC_KEY',key.with_suffix('.pub').read_text().strip())
        (DATA/'user-data').write_text(user_data)
        (DATA/'meta-data').write_text('instance-id: blueberry-android-001\nlocal-hostname: blueberry-android\n')
        subprocess.run(['cloud-localds',str(seed),str(DATA/'user-data'),str(DATA/'meta-data')],check=True)
    import ipaddress
    bind=str(ipaddress.IPv4Address(os.environ.get('BLUEBERRY_BIND_HOST','127.0.0.1')))
    ssh_port=int(os.environ.get('BLUEBERRY_SSH_PORT','2222'));adb_port=int(os.environ.get('BLUEBERRY_ADB_PORT','6555'))
    if not (1<=ssh_port<=65535 and 1<=adb_port<=65535):raise ValueError('Invalid guest forwarding port')
    print(f'Starting persistent Android VM. First boot installs its runtime. SSH listens on {bind}:{ssh_port} and ADB on {bind}:{adb_port}.',flush=True)
    os.execvp('qemu-system-x86_64',['qemu-system-x86_64','-enable-kvm','-cpu','host','-smp','2','-m','6144',
        '-drive',f'file={disk},if=virtio,format=qcow2','-drive',f'file={seed},if=virtio,format=raw',
        '-netdev',f'user,id=net,hostfwd=tcp:{bind}:{ssh_port}-:22,hostfwd=tcp:{bind}:{adb_port}-:5555',
        '-device','virtio-net-pci,netdev=net','-display','none','-serial',f'file:{DATA}/console.log','-monitor','none'])


if __name__=='__main__':main()
