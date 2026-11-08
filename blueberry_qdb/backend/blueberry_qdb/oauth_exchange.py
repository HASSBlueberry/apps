"""Manual PKCE authorization-code exchange for the MyQ Android client."""
import asyncio
import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import ClientError, ClientSession

IDENTITY='https://partner-identity.myq-cloud.com'
CLIENT_ID='ANDROID_CGI_MYQ'
REDIRECT_URI='com.myqops://android'
SCOPE='MyQ_Residential offline_access'


class OAuthExchangeError(ValueError):
    """Safe OAuth error code without callback or credential values."""


class OAuthExchange:
    def __init__(self, service):
        self.service=service
        self.pending=None
        self.lock=asyncio.Lock()

    def begin(self):
        verifier=secrets.token_urlsafe(48)
        state=secrets.token_urlsafe(32)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        query=urlencode({'client_id':CLIENT_ID,'redirect_uri':REDIRECT_URI,'response_type':'code',
                         'scope':SCOPE,'code_challenge':challenge,'code_challenge_method':'S256',
                         'state':state,'prompt':'login','acr_values':'unified_flow:v1 brand:myq'})
        self.pending={'state':state,'verifier':verifier,'created_at':time.time()}
        return {'authorize_url':IDENTITY+'/connect/authorize?'+query,'state':state}

    def _callback(self, value):
        try:
            if not isinstance(value,str) or len(value)>16000:raise ValueError
            parsed=urlsplit(value.strip())
            if (parsed.scheme,parsed.netloc,parsed.path)!=('com.myqops','android','') or parsed.fragment:raise ValueError
            query=parse_qs(parsed.query,keep_blank_values=True)
            if any(len(query.get(key,[]))!=1 for key in ('state',)):raise ValueError
            if any(len(query[key])!=1 for key in ('code','error','iss') if key in query):raise ValueError
            if query.get('iss') and query['iss'][0].rstrip('/')!=IDENTITY:raise ValueError
            if query.get('error'):raise OAuthExchangeError('oauth_access_denied')
            if not query.get('code',[''])[0]:raise ValueError
            return query
        except OAuthExchangeError:
            raise
        except (TypeError,ValueError,AttributeError):
            raise OAuthExchangeError('invalid_callback') from None

    async def exchange(self, callback_url):
        async with self.lock:
            pending=self.pending
            if not pending or time.time()-pending['created_at']>900:
                self.pending=None
                raise OAuthExchangeError('oauth_session_expired')
            query=self._callback(callback_url)
            if not secrets.compare_digest(query['state'][0].encode(),pending['state'].encode()):
                raise OAuthExchangeError('oauth_state_mismatch')
            app_check=self.service.state.get('cache',{})
            if app_check.get('expires_at',0)<=time.time()+30 or not app_check.get('token'):
                raise OAuthExchangeError('app_check_unavailable')
            form={'client_id':CLIENT_ID,'grant_type':'authorization_code','code':query['code'][0],
                  'code_verifier':pending['verifier'],'scope':SCOPE,'redirect_uri':REDIRECT_URI}
            headers={'User-Agent':'BlueberryQ/0.6.1','Firebase-AppCheck-Token':app_check['token']}
            self.pending=None
            payload=await self._post(form,headers)
            self._save(payload)
            return self.public_tokens()

    async def refresh(self):
        async with self.lock:
            credentials=self.service.state.get('oauth',{})
            if not credentials.get('refresh_token'):
                raise OAuthExchangeError('oauth_refresh_token_missing')
            payload=await self._post({'client_id':CLIENT_ID,'grant_type':'refresh_token',
                                      'refresh_token':credentials['refresh_token']},
                                     {'User-Agent':'BlueberryQ/0.6.1'})
            self._save(payload)
            return self.public_tokens()

    async def _post(self, form, headers):
        try:
            async with ClientSession() as session:
                async with session.post(IDENTITY+'/connect/token',data=form,headers=headers,
                                        timeout=30,allow_redirects=False) as response:
                    if response.status!=200:
                        try:body=await response.json(content_type=None)
                        except (ValueError,AttributeError):body={}
                        error=body.get('error') if isinstance(body,dict) else None
                        code=body.get('code') if isinstance(body,dict) else None
                        if code in ('401.122','401.122i'):raise OAuthExchangeError('oauth_application_unauthorized')
                        if error=='invalid_grant':raise OAuthExchangeError('oauth_grant_rejected')
                        if response.status==429:raise OAuthExchangeError('oauth_rate_limited')
                        if response.status>=500:raise OAuthExchangeError('oauth_provider_unavailable')
                        raise OAuthExchangeError('oauth_exchange_rejected')
                    payload=await response.json()
            if not isinstance(payload,dict) or not isinstance(payload.get('access_token'),str):
                raise OAuthExchangeError('oauth_response_invalid')
            if not isinstance(payload.get('expires_in'),(int,float)) or isinstance(payload.get('expires_in'),bool):
                raise OAuthExchangeError('oauth_response_invalid')
            return payload
        except OAuthExchangeError:
            raise
        except (ClientError,TimeoutError,ValueError,TypeError):
            raise OAuthExchangeError('oauth_provider_unavailable') from None

    def _save(self,payload):
        old=self.service.state.get('oauth',{})
        access=payload.get('access_token')
        refresh=payload.get('refresh_token') or old.get('refresh_token') or ''
        self.service.state['oauth']={'access_token':access,'refresh_token':refresh,
            'expires_at':time.time()+max(0,int(payload['expires_in'])),'updated_at':time.time(),
            'token_type':str(payload.get('token_type','Bearer'))[:32]}
        from .server import save
        save(self.service.path,self.service.state)

    def public_tokens(self):
        values=self.service.state.get('oauth',{})
        return {key:values.get(key) for key in ('access_token','refresh_token','expires_at','updated_at','token_type')}
