"""Bounded ADB discovery and extraction from the official app's runtime logs."""
import asyncio
import base64
from contextlib import suppress
import json
import re
import time

PACKAGE = 'com.chamberlain.android.liftmaster.myq'
SERIAL = re.compile(r'[A-Za-z0-9_.:\[\]-]{1,200}\Z')
TARGET = re.compile(r'(?:[A-Za-z0-9][A-Za-z0-9._-]*|\[[0-9a-fA-F:]+\]):([0-9]{1,5})\Z')
TOKEN = re.compile(r'New AppCheck token is acquired: .*?token = ([A-Za-z0-9_.-]{20,8192})')


class ADBError(ValueError):
    """Only stable reason codes may leave the subprocess boundary."""


async def command(*args, binary=False, timeout=20):
    process = await asyncio.create_subprocess_exec('adb', *args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(timeout):
            output = bytearray()
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > 8 * 1024 * 1024:
                    raise ADBError('adb_output_too_large')
            await process.wait()
        if process.returncode:
            raise ADBError('adb_command_failed')
        return bytes(output) if binary else output.decode(errors='replace')
    except TimeoutError:
        raise ADBError('adb_timeout') from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def parse_devices(output):
    devices = []
    for line in output.splitlines():
        columns = line.split()
        if len(columns) < 2 or columns[1] not in ('device', 'unauthorized', 'offline', 'no'):
            continue
        if not SERIAL.fullmatch(columns[0]) or columns[0].startswith('-'):
            continue
        attrs = dict(x.split(':', 1) for x in columns[2:] if ':' in x)
        devices.append({'serial': columns[0], 'state': 'no_permissions' if columns[1] == 'no' else columns[1],
                        'model': attrs.get('model', ''), 'usb': 'usb' in attrs})
    return devices


def select_device(devices, selected=''):
    if selected:
        device = next((d for d in devices if d['serial'] == selected), None)
        if device is None:
            raise ADBError('selected_phone_missing')
        if device['state'] != 'device':
            raise ADBError('phone_' + device['state'])
        return device['serial']
    ready = [d for d in devices if d['state'] == 'device']
    if len(ready) != 1:
        raise ADBError('select_one_phone' if len(ready) > 1 else 'authorize_or_connect_phone')
    return ready[0]['serial']


def extract_token(logs, now=None):
    now = time.time() if now is None else now
    for match in reversed(list(TOKEN.finditer(logs))):
        token = match[1]
        try:
            parts = token.split('.')
            if len(parts) != 3:
                continue
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
            expiry = claims.get('exp')
            if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and now + 60 < expiry < now + 8 * 86400:
                return {'token': token, 'expires_at': int(expiry)}
        except (ValueError, TypeError, AttributeError):
            continue
    raise ADBError('no_unexpired_app_check_in_logs')


async def _dump_once(android, serial, path):
    """One uiautomator attempt. Returns '' when the hierarchy is missing."""
    try:
        await android.run('-s', serial, 'shell', 'uiautomator', 'dump', path)
        xml = await android.run('-s', serial, 'exec-out', 'cat', path)
    except (ADBError, OSError):
        return ''
    if not isinstance(xml, str) or not re.search(r'<\?xml|<hierarchy\b', xml):
        return ''
    return xml


async def ui_dump(android, serial, path):
    """Dump the visible UI hierarchy and always remove the temporary file.

    A privileged adbd can start without BOOTCLASSPATH. app_process then dies
    before it runs, so uiautomator exits 0, prints nothing and writes no file.
    That silent failure must not be read as an empty screen: it is retried once
    after returning adbd to its unprivileged state, and only then refused.
    """
    try:
        xml = await _dump_once(android, serial, path)
        if not xml:
            rooted = False
            with suppress(ADBError, OSError):
                rooted = (await android.run('-s', serial, 'shell', 'id', '-u')).strip() == '0'
            if rooted:
                with suppress(ADBError, OSError):
                    await android.unroot(serial)
                xml = await _dump_once(android, serial, path)
        if not xml:
            raise ADBError('android_screen_unreadable')
        return xml
    finally:
        with suppress(ADBError, OSError):
            await android.run('-s', serial, 'shell', 'rm', '-f', path)


class AndroidBridge:
    def __init__(self, runner=command):
        self.run = runner

    async def discover(self, targets=()):
        for target in targets:
            match = TARGET.fullmatch(target)
            if not match or not 1 <= int(match[1]) <= 65535:
                raise ADBError('invalid_adb_target')
            await self.run('connect', target)
        return parse_devices(await self.run('devices', '-l'))

    async def extract(self, serial):
        if not SERIAL.fullmatch(serial) or serial.startswith('-'):
            raise ADBError('invalid_serial')
        try:
            pids = (await self.run('-s', serial, 'shell', 'pidof', PACKAGE)).split()
        except ADBError as error:
            if str(error) == 'adb_command_failed':
                raise ADBError('open_myq_on_phone') from None
            raise
        if not pids or not pids[0].isdecimal():
            raise ADBError('open_myq_on_phone')
        logs = await self.run('-s', serial, 'logcat', '-d', '-b', 'main', '-v', 'epoch', '--pid=' + pids[0], '*:V')
        return extract_token(logs)

    async def open_myq(self, serial):
        if not SERIAL.fullmatch(serial) or serial.startswith('-'):
            raise ADBError('invalid_serial')
        user=(await self.run('-s',serial,'shell','am','get-current-user')).strip()
        if not re.fullmatch(r'[0-9]{1,5}',user):raise ADBError('open_myq_on_phone')
        resolved=await self.run('-s',serial,'shell','cmd','package','resolve-activity','--brief','--user',user,
                                '-a','android.intent.action.MAIN','-c','android.intent.category.LAUNCHER',PACKAGE)
        components=[line.strip() for line in resolved.splitlines()
                    if re.fullmatch(re.escape(PACKAGE)+r'/[A-Za-z0-9_.$]{1,300}',line.strip())]
        if len(components)!=1:raise ADBError('open_myq_on_phone')
        import shlex
        output=await self.run('-s',serial,'shell','am start --user '+user+' -n '+shlex.quote(components[0]))
        if 'Error:' in output:raise ADBError('open_myq_on_phone')

    async def runtime_status(self, serial):
        """Allowlisted Android environment facts, never app data or credentials."""
        if not SERIAL.fullmatch(serial) or serial.startswith('-'):raise ADBError('invalid_serial')
        async def shell(*args):
            return (await self.run('-s',serial,'shell',*args)).strip()
        boot=await shell('getprop','sys.boot_completed')
        uid=await shell('id','-u')
        abi=await shell('getprop','ro.product.cpu.abi')
        version=await shell('getprop','ro.build.version.release')
        try:
            magisk=await shell('/sbin/magisk','-v')
        except ADBError:
            magisk=''
        import shlex
        async def privileged(value):
            return await shell(value if uid=='0' else '/sbin/su -c '+shlex.quote(value))
        root_ok=uid=='0'
        if not root_ok and ':' in serial:
            if (await shell('getprop','ro.hardware'))=='redroid':
                try:root_ok=(await privileged('id -u'))=='0'
                except ADBError:pass
        if root_ok and not magisk:
            try:magisk=await privileged('/sbin/magisk -v')
            except ADBError:pass
        zygisk=None
        if root_ok and magisk:
            import shlex
            try:
                setting=await privileged('/sbin/magisk --sqlite '+shlex.quote("SELECT value FROM settings WHERE key='zygisk';"))
                zygisk='value=1' in setting
            except ADBError:pass
        installed_modules={}
        core_ready=None
        if root_ok and magisk:
            core_ready=(await privileged('if [ -x /data/adb/magisk/busybox ] && [ -f /data/adb/magisk/util_functions.sh ]; then echo ready; fi'))=='ready'
            raw=await privileged('for module in playintegrityfix zygisk_shamiko; do file=/data/adb/modules/$module/module.prop; if [ -f \"$file\" ]; then echo module=$module; grep \"^version=\" \"$file\"; fi; done')
            selected=None
            for line in raw.splitlines():
                if line.startswith('module='):selected=line[7:] if line[7:] in ('playintegrityfix','zygisk_shamiko') else None
                elif selected and line.startswith('version=') and re.fullmatch(r'[A-Za-z0-9_.() -]{1,80}',line[8:]):installed_modules[selected]=line[8:]
        packages=await shell('pm','list','packages')
        installed=set(packages.splitlines())
        return {'boot_completed':boot=='1','adb_root':uid=='0','root_shell':root_ok,'zygisk_configured':zygisk,'magisk_core_ready':core_ready,'installed_modules':installed_modules,
                'abi':abi if re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',abi) else 'unknown',
                'android_version':version if re.fullmatch(r'[0-9.]{1,20}',version) else 'unknown',
                'magisk_version':magisk if re.fullmatch(r'[a-zA-Z0-9_.():+ -]{1,80}',magisk) else None,
                'google_play_services':'package:com.google.android.gms' in installed,
                'myq_installed':'package:'+PACKAGE in installed}


    async def unroot(self, serial):
        """Give adbd back to the shell user so app_process can start again."""
        if not SERIAL.fullmatch(serial) or serial.startswith('-'):raise ADBError('invalid_serial')
        with suppress(ADBError,OSError):
            if (await self.run('-s',serial,'shell','id','-u')).strip()!='0':return
            await self.run('-s',serial,'unroot')
            await self.run('-s',serial,'wait-for-device')

    async def ensure_redroid(self, serial, root=False):
        if not SERIAL.fullmatch(serial) or serial.startswith('-') or ':' not in serial:raise ADBError('redroid_target_required')
        if ':' in serial:
            # A network target is not remembered across adb server restarts.
            with suppress(ADBError,OSError):
                await self.run('connect',serial)
        await self.run('-s',serial,'wait-for-device')
        hardware=(await self.run('-s',serial,'shell','getprop','ro.hardware')).strip()
        if hardware!='redroid':raise ADBError('redroid_target_required')
        if root:
            if (await self.run('-s',serial,'shell','id','-u')).strip()=='0':return
            try:
                if (await self.run('-s',serial,'shell',"/sbin/su -c 'id -u'")).strip()=='0':return
            except ADBError:pass
            await self.run('-s',serial,'root')
            for _ in range(5):
                await asyncio.sleep(1)
                try:
                    if (await self.run('-s',serial,'shell','id','-u')).strip()=='0':return
                except ADBError:pass
            raise ADBError('android_root_required')

    async def screenshot(self, serial):
        await self.ensure_redroid(serial)
        png=await self.run('-s',serial,'exec-out','screencap','-p',binary=True)
        if not isinstance(png,bytes) or not png.startswith(b'\x89PNG\r\n\x1a\n'):raise ADBError('android_screen_unavailable')
        return png

    async def input_event(self, serial, data):
        await self.ensure_redroid(serial)
        action=data.get('action')
        if action=='tap':
            x,y=data.get('x'),data.get('y')
            if any(not isinstance(v,int) or isinstance(v,bool) or not 0<=v<10000 for v in (x,y)):raise ADBError('invalid_android_coordinates')
            await self.run('-s',serial,'shell','input','tap',str(x),str(y))
        elif action=='key':
            key={'back':'4','home':'3','enter':'66','delete':'67'}.get(data.get('key'))
            if not key:raise ADBError('invalid_android_key')
            await self.run('-s',serial,'shell','input','keyevent',key)
        elif action=='text':
            import shlex
            text=data.get('text')
            if not isinstance(text,str) or not 1<=len(text)<=256 or '%s' in text or any(ord(c)<32 or ord(c)>126 for c in text):raise ADBError('android_text_unsupported')
            await self.run('-s',serial,'shell','input text '+shlex.quote(text.replace(' ','%s')))
        else:raise ADBError('invalid_android_action')
        return {'sent':True}


    async def root_shell(self, serial, command, timeout=20):
        import shlex
        if (await self.run('-s',serial,'shell','id','-u')).strip()=='0':
            return await self.run('-s',serial,'shell',command,timeout=timeout)
        return await self.run('-s',serial,'shell','/sbin/su -c '+shlex.quote(command),timeout=timeout)
