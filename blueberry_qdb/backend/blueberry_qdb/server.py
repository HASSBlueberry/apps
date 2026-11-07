"""Private App Check API and Supervisor ingress for the packaged ADB bridge."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import secrets
import time
import uuid

from aiohttp import ClientError, ClientSession, web
from .adb import ADBError, AndroidBridge, command, select_device


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(temporary, flags, 0o600)
    with os.fdopen(fd, 'w') as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(value, handle)
    temporary.replace(path)


def migrate_state(value):
    version = value.get('version', 1)
    if version not in (1, 2):
        raise ValueError('unsupported_state_version')
    return {'version': 2, 'id': value.get('id') or str(uuid.uuid4()),
            'api_key': value.get('api_key') or secrets.token_urlsafe(32),
            'serial': value.get('serial', ''), 'cache': value.get('cache', {})}


class BridgeService:
    def __init__(self, directory, options=None, android=None):
        self.directory = Path(directory)
        self.path = self.directory / 'state.json'
        self.state = migrate_state(json.loads(self.path.read_text()) if self.path.exists() else {})
        save(self.path, self.state)
        self.options = options or {}
        self.android = android or AndroidBridge()
        self.devices = []
        self.error = None
        self.lock = asyncio.Lock()
        self.csrf = secrets.token_urlsafe(32)
        self.discovery_uuid = None
        self.supervisor = None

    def status(self):
        cache = self.state['cache']
        serial = self.options.get('serial') or self.state['serial']
        usable = cache.get('serial') == serial and cache.get('expires_at', 0) > time.time() + 60
        return {'phones': self.devices, 'selected_serial': serial,
                'app_check_available': bool(usable),
                'expires_at': cache.get('expires_at') if usable else None, 'error': self.error}

    async def scan(self):
        async with self.lock:
            try:
                self.devices = await self.android.discover(self.options.get('adb_targets', []))
                selected = select_device(self.devices, self.options.get('serial') or self.state['serial'])
                if selected != self.state['serial']:
                    self.state['serial'] = selected
                    self.state['cache'] = {}
                self.state['cache'] = {**await self.android.extract(selected), 'serial': selected}
                self.error = None
                save(self.path, self.state)
            except (ADBError, OSError) as error:
                self.error = str(error) if isinstance(error, ADBError) else 'adb_unavailable'
            return self.status()

    async def supervisor_request(self, method, path, payload=None):
        token = os.environ.get('SUPERVISOR_TOKEN')
        if not token:
            return None
        try:
            async with self.supervisor.request(method, 'http://supervisor' + path, json=payload,
                                                headers={'Authorization': 'Bearer ' + token},
                                                timeout=10, allow_redirects=False) as response:
                if response.status >= 300:
                    return None
                return await response.json()
        except (ClientError, TimeoutError, ValueError):
            return None

    async def announce(self):
        if self.discovery_uuid:
            return
        # Own-app info supplies the Supervisor network hostname, not a guessed
        # repository prefix. New Supervisors use apps, older ones use addons.
        info = await self.supervisor_request('GET', '/apps/self/info')
        if not info:
            info = await self.supervisor_request('GET', '/addons/self/info')
        host = (info or {}).get('data', {}).get('hostname')
        if not host:
            return
        result = await self.supervisor_request('POST', '/discovery', {
            'service': 'blueberry_q', 'config': {
                'app_bridge_url': f'http://{host}:8091', 'app_bridge_key': self.state['api_key'],
                'bridge_id': self.state['id'],
            }})
        self.discovery_uuid = (result or {}).get('data', {}).get('uuid')

    async def poll(self):
        while True:
            await self.scan()
            await self.announce()
            await asyncio.sleep(max(15, min(3600, int(self.options.get('poll_seconds', 60)))))


UI_PATHS = {'/', '/status', '/scan', '/select', '/open-myq', '/connection'}


def create_app(service):
    @web.middleware
    async def protect(request, handler):
        supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
        authenticated = secrets.compare_digest(supplied.encode(), service.state['api_key'].encode())
        ingress = request.remote == '172.30.32.2' and request.path in UI_PATHS
        if not authenticated and not ingress:
            raise web.HTTPUnauthorized()
        if ingress and request.method != 'GET' and not authenticated:
            if not secrets.compare_digest(request.headers.get('X-QDB-CSRF', '').encode(), service.csrf.encode()):
                raise web.HTTPForbidden()
        try:
            response = await handler(request)
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Referrer-Policy'] = 'no-referrer'
            return response
        except ADBError as error:
            return web.json_response({'error': str(error)}, status=409)

    app = web.Application(middlewares=[protect], client_max_size=16384)
    async def home(request):
        html = (Path(__file__).parent / 'index.html').read_text().replace('__CSRF__', service.csrf)
        return web.Response(text=html, content_type='text/html')
    async def status(request):
        return web.json_response(service.status())
    async def scan(request):
        return web.json_response(await service.scan())
    async def select(request):
        data = await request.json()
        async with service.lock:
            serial = str(data.get('serial', ''))
            if not any(d['serial'] == serial for d in service.devices):
                raise ADBError('selected_phone_missing')
            if service.options.get('serial') and serial != service.options['serial']:
                raise ADBError('phone_pinned_in_app_options')
            service.state.update(serial=serial, cache={})
            save(service.path, service.state)
        return web.json_response(await service.scan())
    async def open_myq(request):
        async with service.lock:
            serial = select_device(service.devices, service.options.get('serial') or service.state['serial'])
            await service.android.open_myq(serial)
        return web.json_response({'opened': True})
    async def token(request):
        if not service.status()['app_check_available']:
            await service.scan()
        if not service.status()['app_check_available']:
            return web.json_response({'error': service.error or 'app_check_unavailable'}, status=503)
        cache = service.state['cache']
        return web.json_response({'token': cache['token'], 'expires_at': cache['expires_at']})
    async def connection(request):
        # Only authenticated ingress users and API key holders may view pairing
        # details. This endpoint never includes the MyQ attestation itself.
        return web.json_response({'app_bridge_key': service.state['api_key'], 'port': 8091})
    app.router.add_get('/', home)
    app.router.add_get('/status', status)
    app.router.add_post('/scan', scan)
    app.router.add_post('/select', select)
    app.router.add_post('/open-myq', open_myq)
    app.router.add_get('/connection', connection)
    app.router.add_get('/v1/app-check', token)
    return app


async def lifecycle(app):
    service = app['service']
    keys = service.directory / 'adb'
    keys.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ['ADB_VENDOR_KEYS'] = str(keys / 'adbkey')
    if not (keys / 'adbkey').exists():
        await command('keygen', str(keys / 'adbkey'))
        (keys / 'adbkey').chmod(0o600)
    await command('start-server')
    async with ClientSession() as session:
        service.supervisor = session
        poll = asyncio.create_task(service.poll())
        try:
            yield
        finally:
            poll.cancel()
            with suppress(asyncio.CancelledError):
                await poll
            if service.discovery_uuid:
                await service.supervisor_request('DELETE', '/discovery/' + service.discovery_uuid)


def main():
    directory = Path(os.environ.get('QDB_DATA', '/data'))
    options_file = directory / 'options.json'
    options = json.loads(options_file.read_text()) if options_file.exists() else {}
    service = BridgeService(directory, options)
    app = create_app(service)
    app['service'] = service
    app.cleanup_ctx.append(lifecycle)
    web.run_app(app, port=8091, access_log=None)


if __name__ == '__main__':
    main()
