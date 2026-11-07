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
    def __init__(self, websocket):
        self.websocket = websocket
        self.pending = {}
        self.sequence = 0
        self.events = asyncio.Queue()

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
            result = await asyncio.wait_for(future, 15)
            if 'error' in result:
                raise PhoneLoginError('phone_browser_command_failed')
            return result.get('result', {})
        finally:
            self.pending.pop(sequence, None)


class PhoneBrowser:
    def __init__(self, android):
        self.android = android

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
                for _ in range(30):
                    try:
                        async with session.get(origin+'/json/list',timeout=3) as response:
                            targets = await response.json()
                        target = next((t for t in targets if t.get('type') == 'page' and t.get('url') == marker), None)
                        if target:
                            break
                    except (ClientError, TimeoutError, ValueError):
                        pass
                    await asyncio.sleep(.5)
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
        cdp = CDP(websocket)
        reader = asyncio.create_task(cdp.read())
        navigation = None
        try:
            await cdp.call('Page.enable')
            await cdp.call('Fetch.enable',{'patterns':[{'urlPattern':'https://'+IDENTITY+'/*','requestStage':'Response'}]})
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
                    return validate_callback(location,state)
                await cdp.call('Fetch.continueRequest',{'requestId':params['requestId']})
        finally:
            if navigation:
                if not navigation.done():
                    navigation.cancel()
                with suppress(asyncio.CancelledError, PhoneLoginError, TimeoutError):
                    await navigation
            with suppress(PhoneLoginError, TimeoutError):
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


class LoginManager:
    def __init__(self, android, browser=None, ttl=600):
        self.android = android
        self.browser = browser or PhoneBrowser(android)
        self.ttl = ttl
        self.current = None

    def summary(self):
        login = self.current
        if not login:
            return {'status':'idle'}
        if time.time() >= login.expires_at:
            return {'status':'expired'}
        return {'status':login.status,'error':login.error}

    async def start(self, serial, authorize_url):
        state = validate_authorize_url(authorize_url)
        if self.current and self.current.status in ('waiting','ready') and time.time() < self.current.expires_at:
            raise PhoneLoginError('phone_login_in_progress')
        await self.close()
        login = Login(secrets.token_urlsafe(32),time.time()+self.ttl)
        self.current = login
        login.task = asyncio.create_task(self.run(login,serial,authorize_url,state))
        return {'id':login.id,'status':login.status,'expires_at':login.expires_at}

    async def run(self, login, serial, authorize_url, state):
        try:
            async with asyncio.timeout(self.ttl):
                callback = await self.browser.capture(serial,authorize_url,state)
                login.callback = validate_callback(callback,state)
                login.status = 'ready'
        except asyncio.CancelledError:
            login.status = 'cancelled'
            login.callback = None
            raise
        except TimeoutError:
            login.status = 'failed'
            login.error = 'phone_login_timeout'
        except (PhoneLoginError, OSError) as error:
            login.status = 'failed'
            login.error = str(error) if isinstance(error,PhoneLoginError) else 'phone_browser_unavailable'

    async def result(self, session_id):
        login = self.current
        if not login or not secrets.compare_digest(login.id.encode(),session_id.encode()):
            raise PhoneLoginError('phone_login_session_missing')
        if time.time() >= login.expires_at:
            await self.close()
            raise PhoneLoginError('phone_login_timeout')
        result = {'status':login.status,'error':login.error}
        if login.status == 'ready':
            result['callback'] = login.callback
        return result

    async def close(self):
        login, self.current = self.current, None
        if login:
            login.callback = None
            if login.task and not login.task.done():
                login.task.cancel()
            if login.task:
                with suppress(asyncio.CancelledError):
                    await login.task
