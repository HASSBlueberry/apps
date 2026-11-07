"""Verify and install the signed MyQ package on a selected ReDroid instance."""
import asyncio
import hashlib
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from urllib.parse import urlsplit
import zipfile

import aiohttp
from .adb import ADBError, PACKAGE

MYQ_SIGNER='3c49834ec2790eb47233080401a5d37b354bd5ac639beeafbbef7c562005e40c'
LIMIT=300*1024*1024


async def tool(*args):
    process=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(60):
            data=await process.stdout.read(2*1024*1024)
            if len(data)>=2*1024*1024:raise ADBError('apk_tool_output_too_large')
            await process.wait()
        if process.returncode:raise ADBError('apk_verification_failed')
        return data.decode(errors='replace')
    finally:
        if process.returncode is None:process.kill();await process.wait()


def candidates(package,directory):
    with zipfile.ZipFile(package) as archive:
        if 'AndroidManifest.xml' in archive.namelist():return [Path(package)]
        files=[info for info in archive.infolist() if info.filename.lower().endswith('.apk') and not info.is_dir()]
        if not 1<=len(files)<=64 or sum(info.file_size for info in files)>LIMIT:raise ADBError('invalid_apk_bundle')
        result=[]
        for index,info in enumerate(files):
            # Flatten paths to generated names, never extract ZIP paths.
            target=Path(directory)/f'{index}.apk'
            with archive.open(info) as source,target.open('wb') as output:shutil.copyfileobj(source,output,65536)
            target.chmod(0o600);result.append(target)
        return result


async def inspect_apk(path):
    signed=await tool('apksigner','verify','--print-certs',str(path))
    signers=re.findall(r'Signer #\d+ certificate SHA-256 digest:\s*([a-fA-F0-9]{64})',signed)
    if not signers or set(value.lower() for value in signers)!={MYQ_SIGNER}:raise ADBError('myq_signer_mismatch')
    manifest=await tool('aapt','dump','badging',str(path))
    package=re.search(r"^package: name='([^']+)'([^\n]*)",manifest,re.M)
    if not package or package[1]!=PACKAGE:raise ADBError('myq_package_required')
    split=re.search(r"\bsplit='([^']+)'",package[2])
    native=re.search(r'^native-code: (.*)$',manifest,re.M)
    sdk=re.search(r"^sdkVersion:'(\d+)'",manifest,re.M)
    version=re.search(r"\bversionName='([^']*)'",package[2])
    return {'path':Path(path),'split':split[1] if split else '',
            'abis':re.findall(r"'([^']+)'",native[1]) if native else [],
            'sdk':int(sdk[1]) if sdk else 1,'version':version[1][:80] if version else None}


class AppInstaller:
    def __init__(self,service):
        self.service=service;self.task=None;self.phase='idle';self.error=None;self.version=None;self.sha256=None

    def summary(self):
        return {'phase':self.phase,'error':self.error,'running':bool(self.task and not self.task.done()),
                'version':self.version,'sha256':self.sha256,'signer_sha256':MYQ_SIGNER}

    async def start(self,serial,source,upload=None):
        if self.task and not self.task.done():raise ADBError('apk_install_running')
        if not isinstance(source,dict):raise ADBError('invalid_apk_source')
        if source.get('url') is not None and (not isinstance(source['url'],str) or len(source['url'])>8192):raise ADBError('invalid_apk_source')
        if source.get('sha256') and (not isinstance(source['sha256'],str) or not re.fullmatch(r'[a-fA-F0-9]{64}',source['sha256'])):raise ADBError('invalid_apk_checksum')
        self.phase='preparing';self.error=None
        self.task=asyncio.create_task(self.run(serial,source,upload))
        return self.summary()

    async def run(self,serial,source,upload=None):
        self.phase='preparing';self.error=None;self.sha256=None;self.version=None
        try:
            async with asyncio.timeout(600),self.service.native.lock:
                android=self.service.android
                await android.ensure_redroid(serial)
                with tempfile.TemporaryDirectory(prefix='myq-apk-',dir=self.service.directory) as temporary:
                    directory=Path(temporary)
                    if upload:
                        package=Path(upload);self.sha256=hashlib.sha256(package.read_bytes()).hexdigest()
                        paths=await asyncio.to_thread(candidates,package,directory)
                    elif source.get('source')=='usb':
                        self.phase='reading_usb_package'
                        phones=[d for d in await android.discover(self.service.targets()) if d.get('usb') and d['state']=='device']
                        if len(phones)!=1:raise ADBError('usb_myq_source_required')
                        source_serial=phones[0]['serial']
                        output=await android.run('-s',source_serial,'shell','pm','path',PACKAGE)
                        paths=[]
                        for line in output.splitlines():
                            remote=line.removeprefix('package:').strip()
                            if not remote.startswith('/data/app/') or not remote.endswith('.apk'):continue
                            target=directory/f'{len(paths)}.apk'
                            await android.run('-s',source_serial,'pull',remote,str(target),timeout=90);target.chmod(0o600);paths.append(target)
                        if not paths:raise ADBError('usb_myq_source_required')
                    else:
                        self.phase='downloading'
                        url=source.get('url','');parsed=urlsplit(url)
                        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:raise ADBError('https_apk_url_required')
                        package=directory/'download.apk'
                        digest=hashlib.sha256();size=0
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url,timeout=aiohttp.ClientTimeout(total=240),max_redirects=5) as response:
                                if response.status!=200 or response.url.scheme!='https':raise ADBError('apk_download_failed')
                                with package.open('wb') as output:
                                    async for chunk in response.content.iter_chunked(65536):
                                        size+=len(chunk)
                                        if size>LIMIT:raise ADBError('apk_download_too_large')
                                        output.write(chunk);digest.update(chunk)
                        self.sha256=digest.hexdigest()
                        if source.get('sha256') and source['sha256'].lower()!=self.sha256:raise ADBError('apk_checksum_failed')
                        paths=await asyncio.to_thread(candidates,package,directory)
                    self.phase='verifying_publisher'
                    inspected=[await inspect_apk(path) for path in paths]
                    base=[row for row in inspected if not row['split']]
                    if len(base)!=1:raise ADBError('apk_base_required')
                    self.version=base[0]['version']
                    supported=(await android.run('-s',serial,'shell','getprop','ro.product.cpu.abilist')).strip().split(',')
                    sdk=int((await android.run('-s',serial,'shell','getprop','ro.build.version.sdk')).strip())
                    if base[0]['sdk']>sdk:raise ADBError('apk_android_version_unsupported')
                    preferred=next((abi for abi in supported if any(abi in row['abis'] for row in inspected)),None)
                    selected=[row['path'] for row in inspected if not row['abis'] or preferred in row['abis']]
                    if any(row['abis'] for row in inspected) and preferred is None:raise ADBError('apk_abi_unsupported')
                    if base[0]['path'] not in selected:raise ADBError('apk_abi_unsupported')
                    self.phase='installing'
                    command='install-multiple' if len(selected)>1 else 'install'
                    await android.run('-s',serial,command,'-r',*[str(path) for path in selected],timeout=180)
                    installed=await android.run('-s',serial,'shell','pm','path',PACKAGE)
                    if 'package:' not in installed:raise ADBError('myq_install_not_confirmed')
                    self.phase='installed'
        except asyncio.CancelledError:self.phase='cancelled';raise
        except ADBError as error:self.phase='failed';self.error=str(error)
        except (OSError,ValueError,TimeoutError,aiohttp.ClientError,zipfile.BadZipFile):self.phase='failed';self.error='myq_install_failed'
        finally:
            if upload:
                with __import__('contextlib').suppress(OSError):Path(upload).unlink()
