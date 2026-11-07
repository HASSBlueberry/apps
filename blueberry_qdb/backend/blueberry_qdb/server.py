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
from .modules import ModuleSetup
from .apps import AppInstaller, LIMIT
import tempfile
from .adb import TARGET
from .recovery import LoginRecovery, RecoveryError
from .native import NativeController, NativeError


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
    if version not in (1, 2, 3):
        raise ValueError('unsupported_state_version')
    return {'version': 3, 'id': value.get('id') or str(uuid.uuid4()),
            'api_key': value.get('api_key') or secrets.token_urlsafe(32),
            'serial': value.get('serial', ''), 'cache': value.get('cache', {}),
            'native_doors':value.get('native_doors',[]),
            **({'managed_adb_target':value['managed_adb_target']} if value.get('managed_adb_target') else {})}


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
        self.recovery=LoginRecovery(self)
        self.modules=ModuleSetup(self)
        self.apps=AppInstaller(self)
        self.last_recovery_attempt=0
        self.ui_active_until=0
        self.native=NativeController(self,save)

    def targets(self):
        targets=list(self.options.get('adb_targets',[]))
        if self.state.get('managed_adb_target'):targets.append(self.state['managed_adb_target'])
        return list(dict.fromkeys(targets))

    def status(self):
        cache = self.state['cache']
        serial = self.options.get('serial') or self.state['serial']
        usable = cache.get('serial') == serial and cache.get('expires_at', 0) > time.time() + 60
        return {'phones': self.devices, 'selected_serial': serial,
                'app_check_available': bool(usable),
                'login_recovery':self.recovery.summary(),
                'login_recovery_enabled':bool(self.options.get('login_recovery_enabled')),
                'app_check_saved':bool(cache.get('token')),
                'app_check_expires_at':cache.get('expires_at'),
                'expires_at': cache.get('expires_at') if usable else None, 'error': self.error,
                'phone_login':self.logins.summary(),
                'app_check_refreshing':bool(self.refresh_task and not self.refresh_task.done()),
                'native_binding_count':len(self.state['native_doors'])}

    async def scan(self):
        async with self.lock:
            try:
                self.devices = await self.android.discover(self.targets())
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
        if self.recovery.task and not self.recovery.task.done():return
        if login.get('status')=='waiting':
            return  # Never switch away from Chrome during an active sign-in.
        if self.refresh_task and not self.refresh_task.done():
            return
        if automatic and self.last_refresh_attempt is not None and time.monotonic()-self.last_refresh_attempt<300:
            return
        self.last_refresh_attempt=time.monotonic()
        async def refresh():
            try:
                devices=await self.android.discover(self.targets())
                serial=select_device(devices,self.options.get('serial') or self.state['serial'])
                if self.logins.summary().get('status')=='waiting':return
                if self.native.lock.locked():return
                await self.android.open_myq(serial)
                original=self.state['cache'].get('expires_at',0)
                for _ in range(20):
                    await asyncio.sleep(3)
                    await self.scan()
                    if self.state['cache'].get('expires_at',0)>max(original,time.time()+300):return
                if self.options.get('login_recovery_enabled') and time.monotonic()-self.last_recovery_attempt>1800:
                    self.last_recovery_attempt=time.monotonic()
                    await self.recovery.start()
            except (ADBError,OSError) as error:
                self.error=str(error) if isinstance(error,ADBError) else 'adb_unavailable'
        self.refresh_task=asyncio.create_task(refresh())

    async def maybe_refresh_app_check(self):
        if time.monotonic()<self.ui_active_until:return
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


UI_PATHS = {'/', '/status', '/scan', '/select', '/open-myq', '/connection',
            '/native/inspect','/native/bind','/native/devices','/cancel-cloud-login','/recovery/start','/recovery/cancel','/android/status'}


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
        except (ADBError, PhoneLoginError, NativeError, RecoveryError) as error:
            return web.json_response({'error': str(error)}, status=409)

    app = web.Application(middlewares=[protect], client_max_size=LIMIT+1024*1024)
    async def home(request):
        html = (Path(__file__).parent / 'index.html').read_text().replace('__CSRF__', service.csrf)
        return web.Response(text=html, content_type='text/html')
    async def status(request):
        return web.json_response(service.status())
    async def scan(request):
        return web.json_response(await service.scan())
    async def select(request):
        data = await request.json()
        if service.native.lock.locked():raise NativeError('native_controller_busy')
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
        if service.native.lock.locked():raise NativeError('native_controller_busy')
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
            service.devices=await service.android.discover(service.targets())
            serial=select_device(service.devices,service.options.get('serial') or service.state['serial'])
            service.state.update(serial=serial,cache={**candidate,'serial':serial})
            save(service.path,service.state)
        return web.json_response({'imported':True})
    async def refresh_token(request):
        await service.request_app_check_refresh()
        return web.json_response({'refresh_requested':True})
    async def start_login(request):
        if service.recovery.task and not service.recovery.task.done():raise RecoveryError('phone_login_in_progress')
        data=await request.json()
        url=data.get('authorize_url') if isinstance(data,dict) else None
        async with service.lock:
            service.devices=await service.android.discover(service.targets())
            serial=select_device(service.devices,service.options.get('serial') or service.state['serial'])
            return web.json_response(await service.logins.start(serial,url))
    async def login_result(request):
        return web.json_response(await service.logins.result(request.match_info['session_id']))
    async def cancel_login(request):
        await service.logins.result(request.match_info['session_id'])
        await service.logins.close()
        return web.json_response({'cancelled':True})
    async def native_inspect(request):
        if service.logins.summary()['status'] in ('waiting','ready'):raise PhoneLoginError('phone_login_in_progress')
        data=await request.json()
        return web.json_response(await service.native.inspect(data.get('door_name')))
    async def native_bind(request):
        data=await request.json()
        return web.json_response(await service.native.bind(data.get('inspection'),data.get('control_key'),data.get('gesture','tap')))
    async def native_devices(request):
        return web.json_response({'bridge_id':service.state['id'],'devices':await service.native.devices()})
    async def native_command(request):
        data=await request.json()
        return web.json_response(await service.native.command(request.match_info['binding_id'],data.get('action')))
    async def cancel_cloud(request):
        await service.logins.close()
        return web.json_response({'cancelled':True})
    def android_serial():
        return service.options.get('serial') or service.state['serial']
    async def android_target(request):
        data=await request.json();value=data.get('target') if isinstance(data,dict) else None
        match=TARGET.fullmatch(value) if isinstance(value,str) else None
        if not match or not 1<=int(match[1])<=65535:raise ADBError('invalid_adb_target')
        if service.options.get('serial') and service.options['serial']!=value:raise ADBError('clear_pinned_qdb_serial')
        if service.native.lock.locked():raise ADBError('android_controller_busy')
        async with service.lock:
            if service.state.get('serial')!=value:service.state['cache']={}
            service.state.update(serial=value,managed_adb_target=value)
            save(service.path,service.state)
            service.devices=await service.android.discover(service.targets())
        return web.json_response({'target_saved':True})
    async def android_screenshot(request):
        service.ui_active_until=time.monotonic()+120
        return web.Response(body=await service.android.screenshot(android_serial()),content_type='image/png')
    async def android_input(request):
        service.ui_active_until=time.monotonic()+120
        if service.native.lock.locked():raise ADBError('android_controller_busy')
        data=await request.json()
        if not isinstance(data,dict):raise ADBError('invalid_android_action')
        async with service.native.lock:
            return web.json_response(await service.android.input_event(android_serial(),data))
    async def adb_public_key(request):
        path=service.directory/'adb/adbkey.pub'
        try:key=path.read_text().split()[0]
        except (OSError,IndexError):raise ADBError('adb_identity_not_initialized')
        import re
        if not re.fullmatch(r'[A-Za-z0-9+/=]{100,4096}',key):raise ADBError('adb_identity_not_initialized')
        return web.json_response({'public_key':key+' blueberryqdb'})
    async def android_app_open(request):
        serial=android_serial()
        await service.android.ensure_redroid(serial)
        if service.native.lock.locked():raise ADBError('android_controller_busy')
        service.ui_active_until=time.monotonic()+120
        await service.android.open_myq(serial)
        return web.json_response({'opened':True})
    async def android_app(request):
        data=await request.json()
        if not isinstance(data,dict):raise ADBError('invalid_apk_source')
        return web.json_response(await service.apps.start(android_serial(),data))
    async def android_app_upload(request):
        if service.apps.task and not service.apps.task.done():raise ADBError('apk_install_running')
        reader=await request.multipart();field=await reader.next()
        if field is None or field.name!='file':raise ADBError('apk_file_required')
        fd,path=tempfile.mkstemp(prefix='myq-upload-',suffix='.apk',dir=service.directory)
        try:
            with os.fdopen(fd,'wb') as output:
                size=0
                while chunk:=await field.read_chunk(65536):
                    size+=len(chunk)
                    if size>LIMIT:raise ADBError('apk_download_too_large')
                    output.write(chunk)
            return web.json_response(await service.apps.start(android_serial(),{},path))
        except BaseException:
            with suppress(OSError):Path(path).unlink()
            raise
    async def android_modules(request):
        if service.logins.summary().get('status') in ('waiting','ready'):raise ADBError('phone_login_in_progress')
        return web.json_response(await service.modules.start(android_serial()))
    async def android_status(request):
        metadata={'module_setup':service.modules.summary(),'myq_install':service.apps.summary(),
                  'app_check_available':service.status()['app_check_available'],
                  'app_check_expires_at':service.status()['app_check_expires_at']}
        try:
            devices=await service.android.discover(service.targets())
            serial=select_device(devices,service.options.get('serial') or service.state['serial'])
            status=await service.android.runtime_status(serial)
            return web.json_response({**status,'adb_connected':True,**metadata})
        except ADBError as error:
            # Setup results must remain readable during the reboot they request.
            return web.json_response({'boot_completed':False,'adb_connected':False,
                                      'android_error':str(error),**metadata})
    async def recovery_start(request):
        await service.recovery.start()
        return web.json_response(service.recovery.summary())
    async def recovery_cancel(request):
        if service.recovery.task:
            service.recovery.task.cancel()
            with suppress(asyncio.CancelledError):await service.recovery.task
        return web.json_response(service.recovery.summary())
    async def cleanup(app):
        if service.apps.task:
            service.apps.task.cancel()
            with suppress(asyncio.CancelledError):await service.apps.task
        if service.modules.task:
            service.modules.task.cancel()
            with suppress(asyncio.CancelledError):await service.modules.task
        await recovery_cancel(None)
        await service.logins.close()
        if service.refresh_task:
            service.refresh_task.cancel()
            with suppress(asyncio.CancelledError):await service.refresh_task
    app.on_cleanup.append(cleanup)
    app.router.add_get('/v1/android/adb-public-key',adb_public_key)
    app.router.add_post('/v1/android/target',android_target)
    app.router.add_get('/v1/android/screenshot',android_screenshot)
    app.router.add_post('/v1/android/input',android_input)
    app.router.add_post('/v1/android/modules',android_modules)
    app.router.add_post('/v1/android/myq',android_app)
    app.router.add_post('/v1/android/myq/open',android_app_open)
    app.router.add_post('/v1/android/myq/upload',android_app_upload)
    app.router.add_get('/android/status',android_status)
    app.router.add_get('/v1/android/status',android_status)
    app.router.add_post('/recovery/start',recovery_start)
    app.router.add_post('/recovery/cancel',recovery_cancel)
    app.router.add_post('/v1/recovery/start',recovery_start)
    app.router.add_post('/v1/recovery/cancel',recovery_cancel)
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
    app.router.add_post('/v1/native/inspect',native_inspect)
    app.router.add_post('/v1/native/bind',native_bind)
    app.router.add_get('/v1/native/devices',native_devices)
    app.router.add_post('/v1/native/doors/{binding_id:[a-f0-9]+}/command',native_command)
    app.router.add_post('/native/inspect',native_inspect)
    app.router.add_post('/native/bind',native_bind)
    app.router.add_get('/native/devices',native_devices)
    app.router.add_post('/cancel-cloud-login',cancel_cloud)
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
