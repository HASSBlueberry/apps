"""Install only pinned requested Magisk modules on a selected ReDroid target."""
import asyncio
from contextlib import suppress
import hashlib
from pathlib import Path
import shlex
import time
import zipfile

import aiohttp
from .adb import ADBError

MODULES={
 'playintegrityfix':{'version':'v4.9-inject-s','url':'https://github.com/KOWX712/PlayIntegrityFix/releases/download/v4.9-inject-s/PlayIntegrityFix_v4.9-inject-s.zip','sha256':'bc7a9d92638a6bd90a41b70d3aaf86bc549dd8d3a1e5e901b315a6c425852697'},
 'zygisk_shamiko':{'version':'1.2.5-414','url':'https://github.com/LSPosed/LSPosed.github.io/releases/download/shamiko-414/Shamiko-v1.2.5-414-release.zip','sha256':'308d31b2f52a80e49eb58f46bc4c764a6588a79e4b8d101b44860832023f88b4'},
}


class ModuleSetup:
    def __init__(self,service):
        self.service=service;self.task=None;self.phase='idle';self.error=None

    def summary(self):
        return {'phase':self.phase,'error':self.error,'running':bool(self.task and not self.task.done()),
                'versions':{key:value['version'] for key,value in MODULES.items()}}

    async def start(self,serial):
        if self.task and not self.task.done():raise ADBError('module_setup_running')
        self.task=asyncio.create_task(self.run(serial))
        return self.summary()

    async def assets(self,abi):
        if abi not in ('arm64-v8a','armeabi-v7a','x86','x86_64'):raise ADBError('android_architecture_unsupported')
        directory=self.service.directory/'modules';directory.mkdir(mode=0o700,exist_ok=True)
        assets=[]
        async with aiohttp.ClientSession() as session:
            for name,metadata in MODULES.items():
                path=directory/(metadata['sha256']+'.zip')
                if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=metadata['sha256']:
                    async with session.get(metadata['url'],timeout=aiohttp.ClientTimeout(total=120)) as response:
                        if response.status!=200:raise ADBError('module_download_failed')
                        data=bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            data.extend(chunk)
                            if len(data)>32*1024*1024:raise ADBError('module_download_too_large')
                    if hashlib.sha256(data).hexdigest()!=metadata['sha256']:raise ADBError('module_checksum_failed')
                    temporary=path.with_suffix('.tmp');temporary.write_bytes(data);temporary.chmod(0o600);temporary.replace(path)
                # Read the central directory only. No untrusted ZIP paths are extracted.
                with zipfile.ZipFile(path) as archive:
                    if f'zygisk/{abi}.so' not in archive.namelist():
                        raise ADBError('play_integrity_fix_architecture_unsupported' if name=='playintegrityfix' else 'shamiko_architecture_unsupported')
                assets.append(path)
        return assets

    async def run(self,serial):
        self.phase='checking_architecture';self.error=None
        try:
            async with asyncio.timeout(600), self.service.native.lock:
                android=self.service.android
                await android.ensure_redroid(serial)
                status=await android.runtime_status(serial)
                for _ in range(30):
                    if status.get('magisk_core_ready') is not False:break
                    await asyncio.sleep(1);status=await android.runtime_status(serial)
                else:raise ADBError('magisk_core_not_ready')
                assets=await self.assets(status['abi'])
                if not status.get('google_play_services'):raise ADBError('google_play_services_required')
                if not status.get('magisk_version'):raise ADBError('magisk_required')
                self.phase='acquiring_root'
                await android.ensure_redroid(serial,root=True)
                self.phase='enabling_zygisk'
                sql="INSERT OR REPLACE INTO settings (key,value) VALUES ('zygisk',1);"
                await android.root_shell(serial,'/sbin/magisk --sqlite '+shlex.quote(sql))
                self.phase='installing_modules'
                for asset in assets:
                    remote='/data/local/tmp/blueberry-module-'+asset.stem+'.zip'
                    try:
                        await android.run('-s',serial,'push',str(asset),remote)
                        await android.root_shell(serial,'/sbin/magisk --install-module '+remote,timeout=180)
                    finally:
                        await android.run('-s',serial,'shell','rm','-f',remote)
                self.phase='configuring_denylist'
                await android.root_shell(serial,'/sbin/magisk --denylist disable')
                existing=await android.root_shell(serial,'/sbin/magisk --denylist ls')
                for package,process in [('com.google.android.gms','com.google.android.gms.unstable'),
                                        ('com.chamberlain.android.liftmaster.myq','com.chamberlain.android.liftmaster.myq')]:
                    if process not in existing:
                        await android.root_shell(serial,f'/sbin/magisk --denylist add {package} {process}')
                # Hand adbd back to the shell user before the reboot. While it is
                # privileged app_process cannot start, so the next MyQ UI command
                # would find no screen; Magisk root stays available through su.
                with suppress(ADBError,OSError):await android.unroot(serial)
                await android.run('-s',serial,'reboot')
                self.phase='reboot_requested'
        except asyncio.CancelledError:self.phase='cancelled';raise
        except ADBError as error:self.phase='failed';self.error=str(error)
        except (OSError,ValueError,TimeoutError,aiohttp.ClientError,zipfile.BadZipFile):self.phase='failed';self.error='module_setup_failed'
        finally:
            if self.phase not in ('reboot_requested','cancelled'):
                with suppress(ADBError,OSError):await self.service.android.unroot(serial)
