"""Carry a HA-owned PKCE flow through Chrome without exporting browser storage."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientError, ClientSession, WSMsgType
from .adb import ADBError, SERIAL

IDENTITY = 'partner-identity.myq-cloud.com'
CALLBACK = 'com.myqops://android'


class PhoneLoginError(ValueError):
    """Stable error codes only, never debugger messages or callback URLs."""


def validate_authorize_url(url):
    try:
        parsed = urlsplit(url)
        if len(url) > 12000 or parsed.scheme != 'https' or parsed.netloc != IDENTITY or parsed.path != '/connect/authorize' or parsed.fragment:
            raise ValueError
        fields = parse_qs(parsed.query, keep_blank_values=True)
        for key in ('state','code_challenge','code_challenge_method','client_id','redirect_uri','response_type'):
            if len(fields.get(key, [])) != 1:
                raise ValueError
        if fields['redirect_uri'][0] != CALLBACK or fields['response_type'][0] != 'code' or fields['code_challenge_method'][0] != 'S256':
            raise ValueError
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', fields['code_challenge'][0]) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', fields['state'][0]):
            raise ValueError
        if not 1 <= len(fields['client_id'][0]) <= 128:
            raise ValueError
        return fields['state'][0]
    except (ValueError, TypeError, AttributeError):
        raise PhoneLoginError('invalid_authorize_url') from None


def validate_callback(url, state):
    try:
        if not isinstance(url,str) or len(url)>16000:
            raise ValueError
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc, parsed.path) != ('com.myqops','android','') or parsed.fragment:
            raise ValueError
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(query.get(k, [])) != 1 for k in ('state',)):
            raise ValueError
        if not secrets.compare_digest(query['state'][0].encode(), state.encode()):
            raise ValueError
        if any(len(query[k]) != 1 for k in ('code','error','iss') if k in query):
            raise ValueError
        if not query.get('code', [''])[0] and not query.get('error', [''])[0]:
            raise ValueError
        if 'iss' in query and query['iss'][0].rstrip('/') != 'https://' + IDENTITY:
            raise ValueError
        return url
    except (ValueError, TypeError, AttributeError):
        raise PhoneLoginError('phone_callback_mismatch') from None


class CDP:
    def __init__(self, websocket, command_timeout=30):
        self.websocket = websocket
        self.pending = {}
        self.sequence = 0
        self.events = asyncio.Queue()
        self.command_timeout=command_timeout

    async def read(self):
        try:
            async for message in self.websocket:
                if message.type != WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                if 'id' in payload:
                    future = self.pending.pop(payload['id'], None)
                    if future and not future.done():
                        future.set_result(payload)
                else:
                    await self.events.put(payload)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(PhoneLoginError('phone_browser_disconnected'))
            self.events.put_nowait({'method':'Browser.disconnected'})

    async def call(self, method, params=None):
        self.sequence += 1
        sequence = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[sequence] = future
        try:
            await self.websocket.send_json({'id':sequence,'method':method,'params':params or {}})
            result = await asyncio.wait_for(future, self.command_timeout)
            if 'error' in result:
                raise PhoneLoginError('phone_browser_command_failed')
            return result.get('result', {})
        finally:
            self.pending.pop(sequence, None)


class PhoneBrowser:
    def __init__(self, android):
        self.android = android
        self.on_user_ready=None
        self.on_callback=None
        self.startup_timeout=180
        self.command_timeout=30

    async def capture(self, serial, authorize_url, state):
        if not SERIAL.fullmatch(serial) or serial.startswith('-'):
            raise PhoneLoginError('invalid_serial')
        marker = 'about:blank#blueberryqdb-' + secrets.token_hex(12)
        port = None
        try:
            await self.android.run('-s',serial,'shell','am','start','--user','0','-a','android.intent.action.VIEW','-d',marker,'-p','com.android.chrome')
            value = (await self.android.run('-s',serial,'forward','tcp:0','localabstract:chrome_devtools_remote')).strip()
            if not value.isdecimal() or not 1 <= int(value) <= 65535:
                raise PhoneLoginError('phone_browser_forward_failed')
            port = int(value)
            async with ClientSession() as session:
                origin = f'http://127.0.0.1:{port}'
                target = None
                deadline=time.monotonic()+self.startup_timeout
                while time.monotonic()<deadline:
                    try:
                        async with session.get(origin+'/json/list',timeout=3) as response:
                            targets = await response.json()
                        target = next((t for t in targets if t.get('type') == 'page' and t.get('url') == marker), None)
                        if not target:
                            # Chrome may strip the fragment from an about:blank
                            # intent. Create an owned target through its debugger
                            # rather than attaching to an unrelated existing tab.
                            async with session.put(origin+'/json/new?'+marker,timeout=5) as response:
                                created=await response.json()
                            if created.get('type')=='page' and created.get('url','').startswith('about:blank'):
                                target=created
                        if target:
                            break
                    except (ClientError, TimeoutError, ValueError):
                        pass
                    await asyncio.sleep(1)
                if not target:
                    raise PhoneLoginError('phone_chrome_not_ready')
                target_id = target.get('id','')
                path = urlsplit(target.get('webSocketDebuggerUrl','')).path
                if not re.fullmatch(r'[A-Za-z0-9_-]+',target_id) or not path.startswith('/devtools/page/'):
                    raise PhoneLoginError('phone_browser_target_invalid')
                try:
                    async with session.ws_connect(origin+path) as websocket:
                        return await self.intercept(websocket,authorize_url,state)
                finally:
                    # Close only the uniquely marked tab this login created.
                    with suppress(ClientError, TimeoutError):
                        async with session.get(origin+'/json/close/'+target_id,timeout=3) as response:
                            await response.read()
        except ADBError:
            raise PhoneLoginError('phone_adb_unavailable') from None
        except (ClientError, TimeoutError, ValueError) as error:
            if isinstance(error, PhoneLoginError):
                raise
            raise PhoneLoginError('phone_browser_unavailable') from None
        finally:
            if port is not None:
                with suppress(ADBError, OSError):
                    await self.android.run('-s',serial,'forward','--remove','tcp:'+str(port))

    async def intercept(self, websocket, authorize_url, state):
        cdp = CDP(websocket,self.command_timeout)
        reader = asyncio.create_task(cdp.read())
        navigation = None
        try:
            await cdp.call('Page.enable')
            await cdp.call('Fetch.enable',{'patterns':[{'urlPattern':'https://'+IDENTITY+'/*','requestStage':'Response'}]})
            with suppress(PhoneLoginError,TimeoutError):
                await cdp.call('Page.bringToFront')
            if self.on_user_ready:
                self.on_user_ready()
            # Page.navigate can wait for the intercepted response. Process events
            # concurrently rather than waiting for navigation before continuing.
            navigation = asyncio.create_task(cdp.call('Page.navigate',{'url':authorize_url}))
            while True:
                event = await cdp.events.get()
                if event.get('method') == 'Browser.disconnected':
                    raise PhoneLoginError('phone_browser_disconnected')
                if event.get('method') != 'Fetch.requestPaused':
                    continue
                params = event['params']
                location = next((h['value'] for h in params.get('responseHeaders',[]) if h['name'].lower() == 'location'), '')
                if location.startswith('com.myqops:'):
                    # Abort before the native app can redeem our code with its
                    # unrelated verifier. HA still validates state, URI and issuer.
                    await cdp.call('Fetch.failRequest',{'requestId':params['requestId'],'errorReason':'Aborted'})
                    callback=validate_callback(location,state)
                    if self.on_callback:self.on_callback(callback)
                    return callback
                await cdp.call('Fetch.continueRequest',{'requestId':params['requestId']})
        finally:
            if navigation:
                if not navigation.done():
                    navigation.cancel()
                with suppress(asyncio.CancelledError, PhoneLoginError, TimeoutError):
                    await navigation
            with suppress(PhoneLoginError, TimeoutError):
                async with asyncio.timeout(2):
                    await cdp.call('Page.stopLoading')
                    await cdp.call('Fetch.disable')
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader


@dataclass
class Login:
    id: str
    expires_at: float
    status: str = 'waiting'
    callback: str | None = None
    error: str | None = None
    task: asyncio.Task | None = None
    phase: str = 'opening_chrome'
    failed_phase: str | None = None


class LoginManager:
    def __init__(self, android, browser=None, ttl=900, startup_timeout=180):
        self.android = android
        self.browser = browser or PhoneBrowser(android)
        self.ttl = ttl
        self.startup_timeout=startup_timeout
        self.current = None

    def summary(self):
        login = self.current
        if not login:
            return {'status':'idle'}
        if time.time() >= login.expires_at:
            return {'status':'expired'}
        return {'status':login.status,'error':login.error,'phase':login.phase,
                'failed_phase':login.failed_phase,'expires_at':login.expires_at}

    async def start(self, serial, authorize_url):
        state = validate_authorize_url(authorize_url)
        if self.current and self.current.status in ('waiting','ready') and time.time() < self.current.expires_at:
            raise PhoneLoginError('phone_login_in_progress')
        await self.close()
        login = Login(secrets.token_urlsafe(32),time.time()+self.startup_timeout)
        self.current = login
        login.task = asyncio.create_task(self.run(login,serial,authorize_url,state))
        return {'id':login.id,'status':login.status,'expires_at':login.expires_at}

    async def run(self, login, serial, authorize_url, state):
        try:
            async with asyncio.timeout(self.startup_timeout) as timer:
                def user_ready():
                    if timer.expired():raise PhoneLoginError('phone_chrome_not_ready')
                    login.phase='waiting_for_user'
                    login.expires_at=time.time()+self.ttl
                    timer.reschedule(asyncio.get_running_loop().time()+self.ttl)
                def callback_ready(callback):
                    login.callback=validate_callback(callback,state)
                    login.status='ready'
                    login.phase='callback_captured'
                    # Publish before tab/ADB cleanup. HA must not lose the
                    # short-lived authorization code waiting for that cleanup.
                    login.expires_at=time.time()+120
                    timer.reschedule(asyncio.get_running_loop().time()+60)
                self.browser.on_user_ready=user_ready
                self.browser.on_callback=callback_ready
                callback = await self.browser.capture(serial,authorize_url,state)
                login.callback = validate_callback(callback,state)
                login.status = 'ready'
                login.phase='callback_captured'
        except asyncio.CancelledError:
            login.status = 'cancelled'
            login.callback = None
            raise
        except TimeoutError:
            login.failed_phase=login.phase
            login.status = 'failed'
            login.error = 'phone_chrome_not_ready' if login.phase=='opening_chrome' else 'phone_login_timeout'
        except (ValueError, OSError, KeyError, TypeError, ClientError) as error:
            login.failed_phase=login.phase
            login.status = 'failed'
            login.error = str(error) if isinstance(error,PhoneLoginError) else 'phone_browser_unavailable'
        finally:
            self.browser.on_user_ready=None
            self.browser.on_callback=None

    async def result(self, session_id):
        login = self.current
        if not login or not secrets.compare_digest(login.id.encode(),session_id.encode()):
            raise PhoneLoginError('phone_login_session_missing')
        if time.time() >= login.expires_at:
            await self.close()
            raise PhoneLoginError('phone_login_timeout')
        result = {'status':login.status,'error':login.error,'phase':login.phase,
                  'failed_phase':login.failed_phase,'expires_at':login.expires_at}
        if login.status == 'ready':
            result['callback'] = login.callback
        return result

    async def close(self):
        login, self.current = self.current, None
        if login:
            login.callback = None
            if login.status!='ready' and login.task and not login.task.done() and not login.task.cancelling():
                login.task.cancel()
            if login.task:
                try:
                    with suppress(asyncio.CancelledError):await login.task
                finally:
                    login.callback=None
