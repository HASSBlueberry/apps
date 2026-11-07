"""Bounded ADB discovery and extraction from the official app's runtime logs."""
import asyncio
import base64
import json
import re
import time

PACKAGE = 'com.chamberlain.android.liftmaster.myq'
SERIAL = re.compile(r'[A-Za-z0-9_.:\[\]-]{1,200}\Z')
TARGET = re.compile(r'(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:]+\]):([0-9]{1,5})\Z')
TOKEN = re.compile(r'New AppCheck token is acquired: .*?token = ([A-Za-z0-9_.-]{20,8192})')


class ADBError(ValueError):
    """Only stable reason codes may leave the subprocess boundary."""


async def command(*args):
    process = await asyncio.create_subprocess_exec('adb', *args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(20):
            output = bytearray()
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > 8 * 1024 * 1024:
                    raise ADBError('adb_output_too_large')
            await process.wait()
        if process.returncode:
            raise ADBError('adb_command_failed')
        return output.decode(errors='replace')
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
        logs = await self.run('-s', serial, 'logcat', '-d', '-v', 'epoch', '-t', '20000', '--pid=' + pids[0])
        return extract_token(logs)

    async def open_myq(self, serial):
        if not SERIAL.fullmatch(serial) or serial.startswith('-'):
            raise ADBError('invalid_serial')
        await self.run('-s', serial, 'shell', 'monkey', '-p', PACKAGE,
                       '-c', 'android.intent.category.LAUNCHER', '1')
