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
from .adb import ADBError, AndroidBridge, command, select_device, extract_token
from .oauth import LoginManager, PhoneLoginError


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
        self.logins = LoginManager(self.android)
        self.refresh_task=None
        self.last_refresh_attempt=None

    def status(self):
        cache = self.state['cache']
        serial = self.options.get('serial') or self.state['serial']
        usable = cache.get('serial') == serial and cache.get('expires_at', 0) > time.time() + 60
        return {'phones': self.devices, 'selected_serial': serial,
                'app_check_available': bool(usable),
                'expires_at': cache.get('expires_at') if usable else None, 'error': self.error,
                'phone_login':self.logins.summary(),
                'app_check_refreshing':bool(self.refresh_task and not self.refresh_task.done())}

    async def scan(self):
        async with self.lock:
            try:
                self.devices = await self.android.discover(self.options.get('adb_targets', []))
                selected = select_device(self.devices, self.options.get('serial') or self.state['serial'])
                if selected != self.state['serial']:
                    self.state['serial'] = selected
                    self.state['cache'] = {}
                candidate={**await self.android.extract(selected),'serial':selected}
                cached=self.state['cache']
                # Old acquisition messages can remain in logcat after a newer
                # token was imported or observed. Never replace a newer cache.
                if cached.get('serial')!=selected or candidate['expires_at']>cached.get('expires_at',0):
                    self.state['cache']=candidate
                self.error = None
                save(self.path, self.state)
            except (ADBError, OSError) as error:
                self.error = str(error) if isinstance(error, ADBError) else 'adb_unavailable'
            return self.status()

    async def request_app_check_refresh(self,automatic=False):
        login=self.logins.summary()
        if login.get('status')=='waiting':
            return  # Never switch away from Chrome during an active sign-in.
        if self.refresh_task and not self.refresh_task.done():
            return
        if automatic and self.last_refresh_attempt is not None and time.monotonic()-self.last_refresh_attempt<300:
            return
        self.last_refresh_attempt=time.monotonic()
        async def refresh():
            try:
                devices=await self.android.discover(self.options.get('adb_targets',[]))
                serial=select_device(devices,self.options.get('serial') or self.state['serial'])
                if self.logins.summary().get('status')=='waiting':return
                await self.android.open_myq(serial)
                await asyncio.sleep(2)
                await self.scan()
            except (ADBError,OSError) as error:
                self.error=str(error) if isinstance(error,ADBError) else 'adb_unavailable'
        self.refresh_task=asyncio.create_task(refresh())

    async def maybe_refresh_app_check(self):
        if not self.options.get('auto_refresh_app_check',True):return
        selected=self.options.get('serial') or self.state['serial']
        if not any(d['state']=='device' and d['serial']==selected for d in self.devices):return
        if self.state['cache'].get('expires_at',0)>time.time()+300:return
        await self.request_app_check_refresh(automatic=True)

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
            await self.maybe_refresh_app_check()
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
        except (ADBError, PhoneLoginError) as error:
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
            if service.logins.summary()['status'] in ('waiting','ready'):
                raise PhoneLoginError('phone_login_in_progress')
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
    async def import_token(request):
        data=await request.json()
        token=data.get('token','') if isinstance(data,dict) else ''
        if not isinstance(token,str):
            raise ADBError('invalid_app_check_token')
        # Use the same expiry checks as runtime extraction; the identity
        # provider remains responsible for validating the signature.
        candidate=extract_token('New AppCheck token is acquired: token = '+token)
        async with service.lock:
            service.devices=await service.android.discover(service.options.get('adb_targets',[]))
            serial=select_device(service.devices,service.options.get('serial') or service.state['serial'])
            service.state.update(serial=serial,cache={**candidate,'serial':serial})
            save(service.path,service.state)
        return web.json_response({'imported':True})
    async def refresh_token(request):
        await service.request_app_check_refresh()
        return web.json_response({'refresh_requested':True})
    async def start_login(request):
        data=await request.json()
        url=data.get('authorize_url') if isinstance(data,dict) else None
        async with service.lock:
            service.devices=await service.android.discover(service.options.get('adb_targets',[]))
            serial=select_device(service.devices,service.options.get('serial') or service.state['serial'])
            return web.json_response(await service.logins.start(serial,url))
    async def login_result(request):
        return web.json_response(await service.logins.result(request.match_info['session_id']))
    async def cancel_login(request):
        await service.logins.result(request.match_info['session_id'])
        await service.logins.close()
        return web.json_response({'cancelled':True})
    async def cleanup(app):
        await service.logins.close()
        if service.refresh_task:
            service.refresh_task.cancel()
            with suppress(asyncio.CancelledError):await service.refresh_task
    app.on_cleanup.append(cleanup)
    app.router.add_get('/', home)
    app.router.add_get('/status', status)
    app.router.add_post('/scan', scan)
    app.router.add_post('/select', select)
    app.router.add_post('/open-myq', open_myq)
    app.router.add_get('/connection', connection)
    app.router.add_get('/v1/app-check', token)
    app.router.add_post('/v1/app-check', import_token)
    app.router.add_post('/v1/app-check/refresh', refresh_token)
    app.router.add_post('/v1/oauth/sessions', start_login)
    app.router.add_get('/v1/oauth/sessions/{session_id:[A-Za-z0-9_-]+}', login_result)
    app.router.add_delete('/v1/oauth/sessions/{session_id:[A-Za-z0-9_-]+}', cancel_login)
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
