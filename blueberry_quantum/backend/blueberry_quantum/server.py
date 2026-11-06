"""Local multi-provider management API and Home Assistant Ingress application."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import time
from aiohttp import ClientSession, CookieJar, TCPConnector, web
from .providers import DRIVERS, ProviderError
from .schema import validate,public_values,interfaces,obj,ID

PUBLIC = Path(__file__).parent / 'public'

class Manager:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'providers.json'
        self.configs = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.drivers, self.sessions, self.locks, self.plans = {}, {}, {}, {}
        tokenfile = self.directory / 'api-token'
        if not tokenfile.exists():
            fd = os.open(tokenfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle: handle.write(secrets.token_urlsafe(32))
        self.token = tokenfile.read_text().strip()
    def save(self):
        temporary = self.path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd,'w') as handle: json.dump(self.configs,handle)
        os.replace(temporary,self.path)
    def public(self):
        return [public_values({key:value for key,value in row.items() if key not in ('password','token')},DRIVERS[row['kind']].configuration_schema()) | {'id':pid} for pid,row in self.configs.items()]
    def definitions(self):return {kind:driver.definition() for kind,driver in DRIVERS.items()}
    async def configure(self,values):
        pid=values.get('id') or secrets.token_hex(8)
        if not isinstance(pid,str) or not __import__('re').fullmatch('[a-zA-Z0-9_-]{1,80}',pid):raise ProviderError('Invalid provider ID')
        if pid not in self.configs and len(self.configs)>=32:raise ProviderError('Maximum 32 providers')
        current=self.configs.get(pid,{})
        kind=values.get('kind',current.get('kind'))
        if kind not in DRIVERS:raise ProviderError('Unknown provider type')
        driver=DRIVERS[kind];schema=driver.configuration_schema()
        incoming={key:value for key,value in values.items() if key not in ('id','kind')}
        config={key:value for key,value in current.items() if key in schema.get('properties',{})} if current.get('kind')==kind else {}
        config.update(incoming)
        if current and (current.get('kind')!=kind or ('url' in incoming and current.get('url')!=incoming['url'].rstrip('/'))):
            secret_fields={key for key,field in schema.get('properties',{}).items() if field.get('writeOnly')} | {'password','token','network_id'}
            for field in secret_fields:
                if field not in incoming:config.pop(field,None)
        config=driver.validate_configuration(validate(config,schema))
        config['kind']=kind
        self.configs[pid]=config
        self.invalidate_plans(pid)
        await self.disconnect(pid)
        self.save()
        return pid
    def invalidate_plans(self,pid):
        self.plans={key:plan for key,plan in self.plans.items() if plan['provider']!=pid}
    async def disconnect(self,pid):
        self.drivers.pop(pid,None)
        session=self.sessions.pop(pid,None)
        if session: await session.close()
    async def driver(self,pid):
        if pid not in self.configs: raise web.HTTPNotFound()
        if pid not in self.drivers:
            config=self.configs[pid]
            session=ClientSession(cookie_jar=CookieJar(unsafe=True),connector=TCPConnector(ssl=False if config.get('verify_tls') is False else ssl.create_default_context()))
            self.sessions[pid]=session
            self.drivers[pid]=DRIVERS[config['kind']](session,config)
        return self.drivers[pid]
    async def snapshot(self,pid):
        driver=await self.driver(pid)
        result=await driver.snapshot()
        result['resources']=interfaces(driver,result)
        result.pop('_schema',None)
        result['actions']=driver.actions(result) if hasattr(driver,'actions') else []
        result['revision']=self.resource_revision(pid,result)
        return result
    def resource_revision(self,pid,snapshot,resource=None,values=None):
        entries=snapshot.get('resources',[])
        if resource is not None:entries=[row for row in entries if row['id']==resource]
        def stable(value):
            if isinstance(value,dict):return {key:stable(item) for key,item in value.items() if key not in ('last_seen','first_seen','last_active','uptime','uptime_ms')}
            if isinstance(value,list):return [stable(item) for item in value]
            return value
        return hashlib.sha256(json.dumps({'resources':stable(entries),'network_id':snapshot.get('status',{}).get('network_id'),'kind':self.configs[pid]['kind'],'url':self.configs[pid]['url']},sort_keys=True).encode()).hexdigest()
    async def close(self):
        for pid in list(self.sessions): await self.disconnect(pid)

@web.middleware
async def errors(request, handler):
    try: return await handler(request)
    except (ProviderError,ValueError,KeyError,TypeError) as error:
        return web.json_response({'error':str(error)},status=400)
    except (TimeoutError, __import__('aiohttp').ClientError):
        return web.json_response({'error':'Provider unreachable or TLS validation failed'},status=502)

@web.middleware
async def authorization(request,handler):
    # Public assets contain no credentials. All management operations require the app token.
    if request.path.startswith('/api/'):
        expected=request.app['manager'].token
        supplied=request.headers.get('Authorization','').removeprefix('Bearer ')
        if not secrets.compare_digest(supplied,expected): raise web.HTTPUnauthorized()
    return await handler(request)

async def create_app(directory='/data/quantum'):
    manager=Manager(directory)
    app=web.Application(middlewares=[errors,authorization],client_max_size=64*1024)
    app['manager']=manager
    async def bootstrap(request):
        # Validate the TCP peer, never a caller-supplied forwarding header.
        if request.remote != '172.30.32.2': raise web.HTTPForbidden()
        return web.json_response({'token':manager.token},headers={'Cache-Control':'no-store'})
    async def providers(request):
        if request.method=='GET': return web.json_response({'providers':manager.public(),'types':list(DRIVERS),'definitions':manager.definitions()})
        values=await request.json()
        pid=values.get('id') or secrets.token_hex(8)
        values['id']=pid
        if not isinstance(pid,str): raise ProviderError('Invalid provider ID')
        async with manager.locks.setdefault(pid,asyncio.Lock()):
            return web.json_response({'id':await manager.configure(values)})
    async def provider(request):
        pid=request.match_info['pid']
        async with manager.locks.setdefault(pid,asyncio.Lock()):
            action=request.match_info.get('action','snapshot')
            if action=='snapshot': return web.json_response(await manager.snapshot(pid))
            if action=='inspect': return web.json_response(await (await manager.driver(pid)).inspect())
            if action=='reconnect':
                manager.invalidate_plans(pid)
                await manager.disconnect(pid)
                return web.json_response(await manager.snapshot(pid))
            if action=='delete':
                if pid not in manager.configs: raise web.HTTPNotFound()
                await manager.disconnect(pid);del manager.configs[pid];manager.save()
                return web.json_response({'deleted':True})
            if action in ('eero_login','eero_verify','eero_select'):
                driver=await manager.driver(pid)
                if driver.kind!='eero': raise ProviderError('Select an Eero provider')
                values=await request.json()
                if action=='eero_login': return web.json_response(await driver.begin_login(values['login']))
                result=await (driver.verify_login(values['code']) if action=='eero_verify' else driver.choose_network(values['network_id']))
                manager.invalidate_plans(pid)
                manager.save()
                return web.json_response(result)
            if action=='invoke':
                values=await request.json();driver=await manager.driver(pid)
                snapshot=await manager.snapshot(pid)
                descriptor=next((row for row in snapshot['actions'] if row['id']==values.get('action')),None)
                if descriptor is None:raise ProviderError('Unknown provider action')
                payload=validate(values.get('values',{}),descriptor['schema'])
                result=await driver.invoke(descriptor['id'],payload)
                manager.invalidate_plans(pid);manager.save()
                return web.json_response(result)
            if action=='plan':
                values=await request.json();resource=values['resource'];operation=values['operation']
                snapshot=await manager.snapshot(pid)
                descriptor=next((row for row in snapshot['resources'] if row['id']==resource),None)
                if descriptor is None or not descriptor['write'] or operation not in descriptor['operations']:raise ProviderError('Provider does not advertise this write operation')
                schema=descriptor.get('operation_schemas',{}).get(operation,obj({'id':ID},['id']) if operation in ('delete','disconnect','reconnect') else descriptor['schema'])
                payload=validate(values['values'],schema)
                if descriptor.get('collection') and payload.get('id') and not any(str(row.get('id'))==str(payload['id']) for row in descriptor['value']):raise ProviderError('Resource no longer exists')
                now=time.time();manager.plans={key:row for key,row in manager.plans.items() if row['expires']>now}
                if len(manager.plans)>=128:raise ProviderError('Too many pending plans')
                plan={'id':secrets.token_urlsafe(24),'provider':pid,'resource':resource,'operation':operation,'values':payload,'revision':manager.resource_revision(pid,snapshot,resource,payload),'expires':now+300}
                if descriptor.get('notice'):plan['notice']=descriptor['notice']
                manager.plans[plan['id']]=plan
                return web.json_response({**plan,'values':public_values(payload,schema)})
            if action=='apply':
                plan=manager.plans.pop((await request.json())['plan_id'],None)
                if not plan or plan['provider']!=pid or plan['expires']<time.time(): raise ProviderError('Plan expired or missing')
                snapshot=await manager.snapshot(pid)
                if manager.resource_revision(pid,snapshot,plan['resource'],plan['values'])!=plan['revision']: raise ProviderError('Provider changed. Review a new plan')
                await (await manager.driver(pid)).apply(plan['resource'],plan['operation'],plan['values'])
                return web.json_response({'submitted':True,'snapshot':await manager.snapshot(pid)})
            if action in ('scan','sync'):
                values=await request.json();target=await manager.driver(values['proxy_id'])
                if target.kind!='blueberry_proxy': raise ProviderError('Select a BlueberryProxy provider')
                snapshot=await manager.snapshot(pid)
                networks=snapshot.get('networks',[])
                if not networks: raise ProviderError('Router did not advertise CIDRs. Set explicit networks in provider settings')
                if action=='sync':
                    return web.json_response(await target.json('/uscan/devices','POST',json={'source':'quantum-'+pid,'cidrs':networks,'devices':snapshot.get('devices',[])}))
                return web.json_response(await target.json('/uscan/jobs','POST',json={'parameters':{'cidrs':networks},'scanner_ids':['arp','mdns','wsd','onvif','avahi']}))
            raise web.HTTPNotFound()
    async def index(request): return web.FileResponse(PUBLIC/'index.html',headers={'Content-Security-Policy':"default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'"})
    async def shutdown(app): await manager.close()
    app.router.add_get('/',index)
    app.router.add_get('/bootstrap',bootstrap)
    app.router.add_get('/api/providers',providers)
    app.router.add_post('/api/providers',providers)
    app.router.add_get('/api/providers/{pid}/snapshot',provider)
    app.router.add_route('GET','/api/providers/{pid}/{action:inspect}',provider)
    app.router.add_post('/api/providers/{pid}/{action:reconnect|delete|plan|apply|invoke|scan|sync|eero_login|eero_verify|eero_select}',provider)
    app.router.add_static('/static/',PUBLIC)
    app.on_cleanup.append(shutdown)
    return app

def main():
    web.run_app(create_app(os.environ.get('QUANTUM_DATA','/data/quantum')),host=os.environ.get('QUANTUM_HOST','0.0.0.0'),port=int(os.environ.get('QUANTUM_PORT','8060')),access_log=None)

if __name__=='__main__': main()
