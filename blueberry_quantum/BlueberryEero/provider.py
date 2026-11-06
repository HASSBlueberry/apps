"""Eero account API adapter based on the user-supplied Android app interfaces.

Cloud-assisted management. This does not claim a local Eero administration API.
"""
from __future__ import annotations
import ipaddress
import json
import re
import time
from urllib.parse import urljoin,urlsplit,quote
from blueberry_quantum.base import Provider,ProviderError,Unsupported,forward_value,dns_settings_value

class EeroProvider(Provider):
    kind='eero'
    title='Eero'
    @classmethod
    def configuration_schema(cls):
        from blueberry_quantum.schema import config_schema,field
        schema=config_schema({'token':field('Existing session token',writeOnly=True),'network_id':field('Network ID')},{'url':'https://api-user.e2ro.com'})
        schema['properties']['verify_tls']['const']=True
        return schema
    @classmethod
    def validate_configuration(cls,config):
        if config['url'] not in cls.allowed_origins or config.get('verify_tls') is False:raise ProviderError('Eero requires the official HTTPS API with certificate verification')
        return config
    def resource_specs(self,snapshot):
        from blueberry_quantum.schema import obj,field,IP,ID,ENABLED
        return {'dmz':{'reason':'The inspected Eero API does not expose a DMZ host setting.'},'devices':{'operations':{'disconnect':'Pause internet access','reconnect':'Restore internet access'},'notice':'Pausing blocks internet access. Wi-Fi associations and local LAN access may remain.'},'lighting':{'collection':True,'columns':['name','enabled'],'schema':obj({'id':ID,'enabled':ENABLED},['id','enabled'])},
                'dhcp':{'schema':obj({'mode':field('Mode',enum=['automatic','custom']),'subnet_ip':field('Subnet address',format='ipv4'),'subnet_mask':field('Subnet mask',format='ipv4'),'start_ip':field('Lease range start',format='ipv4'),'end_ip':field('Lease range end',format='ipv4')},['mode'])}}
    def actions(self,snapshot):
        from blueberry_quantum.schema import obj,field
        return [{'id':'login','title':'Send Eero verification code','schema':obj({'login':field('Email or phone')},['login'])},
                {'id':'verify','title':'Verify Eero code','schema':obj({'code':field('Verification code',writeOnly=True)},['code'])},
                {'id':'select_network','title':'Select network','schema':obj({'network_id':field('Network',enum=[row['id'] for row in snapshot.get('available_networks',[])],enumNames=[row['name'] for row in snapshot.get('available_networks',[])])},['network_id'])}]
    async def invoke(self,action,values):
        if action=='login':return await self.begin_login(values['login'])
        if action=='verify':return await self.verify_login(values['code'])
        if action=='select_network':return await self.choose_network(values['network_id'])
        raise Unsupported('Unknown Eero action')
    allowed_origins={'https://api-user.e2ro.com'}
    def __init__(self,session,config):
        super().__init__(session,config)
        if self.url not in self.allowed_origins:raise ProviderError('Eero credentials require the official HTTPS API origin')
        if config.get('verify_tls') is False:raise ProviderError('Eero API certificate verification must remain enabled')
        self.pending_token=None
        self.pending_until=0
        self.network=None
        self.device_urls={}
        self.light_urls={}
        self.forward_urls={}
    def path(self,value):
        if not isinstance(value,str) or not value or value.startswith('//') or '\\' in value:raise ProviderError('Invalid Eero resource link')
        url=urlsplit(urljoin(self.url+'/',value));origin=urlsplit(self.url)
        if (url.scheme,url.hostname,url.port)!=(origin.scheme,origin.hostname,origin.port) or url.username or url.password or url.fragment:
            raise ProviderError('Eero resource link points outside the authenticated API origin')
        if not url.path.startswith('/2.2/') and not url.path.startswith('/2.3/'):raise ProviderError('Unsupported Eero API resource path')
        return url.path+('?' + url.query if url.query else '')
    async def request(self,path,method='GET',token=None,**kwargs):
        credential=self.config.get('token','') if token is None else token
        headers={'Accept':'application/json'}
        if credential:
            if not isinstance(credential,str) or not re.fullmatch(r'[!-~]{1,4096}',credential):raise ProviderError('Invalid Eero session token')
            headers['X-User-Token']=credential
        text=await self.text(self.path(path),method,headers=headers,**kwargs)
        data=json.loads(text) if text else {}
        if not isinstance(data,dict):raise ProviderError('Invalid Eero API response')
        meta=data.get('meta',{})
        if isinstance(meta,dict) and meta.get('code') is not None and int(meta['code'])>=300:raise ProviderError('Eero API rejected the request')
        return data.get('data',data)
    async def begin_login(self,login):
        login=str(login).strip()
        if not login or len(login)>320:raise ProviderError('Enter the Eero account email or phone number')
        if self.pending_until-time.monotonic()>540:raise ProviderError('A verification code was just requested. Wait before requesting another')
        data=await self.request('/2.2/login','POST',token='',data={'login':login})
        token=data.get('user_token') if isinstance(data,dict) else None
        if not isinstance(token,str) or not token:raise ProviderError('Eero did not return a pending session token')
        self.pending_token=token;self.pending_until=time.monotonic()+600
        return {'message':'Verification code requested. Enter the code delivered by Eero.'}
    async def verify_login(self,code):
        if not self.pending_token or time.monotonic()>=self.pending_until:raise ProviderError('Request a new verification code')
        code=str(code).strip()
        if not re.fullmatch('[0-9A-Za-z-]{4,16}',code):raise ProviderError('Enter the Eero verification code')
        await self.request('/2.2/login/verify','POST',token=self.pending_token,data={'code':code})
        account=await self.request('/2.2/account',token=self.pending_token)
        self.config['token']=self.pending_token;self.pending_token=None;self.pending_until=0
        return {'message':'Eero sign-in complete. Select a network if more than one is available.','networks':self.account_networks(account)}
    def account_networks(self,account):
        rows=account.get('networks',{}) if isinstance(account,dict) else {}
        rows=rows.get('data',[]) if isinstance(rows,dict) else rows
        result=[]
        for row in rows if isinstance(rows,list) else []:
            if not isinstance(row,dict) or not row.get('url'):continue
            path=self.path(row['url'])
            if not re.fullmatch('/2\\.[23]/networks/[A-Za-z0-9_-]+',path):continue
            result.append({'id':path.rsplit('/',1)[-1],'name':row.get('name',row.get('nickname_label','Eero network')),'path':path})
        return result
    async def choose_network(self,network_id):
        networks=self.account_networks(await self.request('/2.2/account'))
        chosen=next((row for row in networks if row['id']==str(network_id)),None)
        if not chosen:raise ProviderError('Choose a network belonging to this Eero account')
        self.config['network_id']=chosen['id'];self.network=None
        return {'selected':chosen['id']}
    async def inspect(self):
        return {'provider':'eero','transport':'HTTPS account API','source':'Supplied Eero Android 26.13.0.36807 APK','auth':'email/phone verification and X-User-Token','dns_host_records':False,'local_control':False}
    async def snapshot(self):
        capabilities={'devices':False,'dns_read':False,'dns_write':False,'forwards_read':False,'forwards_write':False,'dns_settings_read':False,'dns_settings_write':False}
        result={'status':{'model':'Eero','transport':'cloud'},'devices':[],'networks':list(self.config.get('networks',[])),'dns':[],'forwards':[],'capabilities':capabilities,'available_networks':[],
                'dns_note':'Eero exposes upstream DNS settings rather than DNS host-record CRUD. Select BlueberryProxy for local DNS records.'}
        if not self.config.get('token'):
            result['authentication_required']=True;return result
        networks=self.account_networks(await self.request('/2.2/account'))
        result['available_networks']=[{k:v for k,v in row.items() if k!='path'} for row in networks]
        selected=next((row for row in networks if row['id']==str(self.config.get('network_id',''))),None)
        if selected is None and len(networks)==1:selected=networks[0]
        if selected is None:
            result['network_selection_required']=True;return result
        network=await self.request(selected['path'])
        if not isinstance(network,dict):raise ProviderError('Invalid Eero network response')
        self.network=network
        resources=network.get('resources',{})
        result['status'].update(name=network.get('name',selected['name']),network_id=selected['id'],connection=network.get('status'),gateway_ip=network.get('gateway_ip'),wan_ip=network.get('wan_ip'))
        if isinstance(network.get('dns'),dict):
            dns=network['dns'];custom=dns.get('custom') or {};parent=dns.get('parent') or {}
            result['dns_settings']={'mode':dns.get('mode'),'custom_ips':custom.get('ips',[]),'ips':custom.get('ips',[]),'parent_ips':parent.get('ips',[]),'caching':dns.get('caching')}
            capabilities['dns_settings_read']=True
            # DnsViewModel clones the current network and updates DNS through resources.settings.
            capabilities['dns_settings_write']=bool(self.config.get('write_enabled') and resources.get('settings'))
        dhcp=network.get('dhcp') or {};custom=dhcp.get('custom') or {}
        if custom.get('subnet_ip') and custom.get('subnet_mask'):
            try:
                cidr=str(ipaddress.IPv4Network(custom['subnet_ip']+'/'+custom['subnet_mask'],strict=False))
                if cidr not in result['networks']:result['networks'].append(cidr)
            except ValueError:pass
        errors={}
        self.device_urls={}
        if resources.get('devices'):
            try:
                data=await self.request(resources['devices']);rows=data.get('data',[]) if isinstance(data,dict) else data
                for row in rows if isinstance(rows,list) else []:
                    if not isinstance(row,dict) or row.get('connected') is False:continue
                    ip=row.get('ipv4') or row.get('ip')
                    try:
                        address=ipaddress.IPv4Address(ip)
                        if address.is_loopback or address.is_unspecified or address.is_multicast:continue
                    except (ValueError,TypeError):continue
                    device_id=str(row.get('mac') or row.get('url','').rsplit('/',1)[-1] or address)
                    if row.get('url'):
                        path=self.path(row['url'])
                        if path.startswith(self.path(resources['devices']).rstrip('/')+'/'):self.device_urls[device_id]=path
                    result['devices'].append({'id':device_id,'connected':True,'blocked':bool(row.get('paused') or row.get('blacklisted')),'ip':str(address),'mac':row.get('mac',row.get('mac_address')),'hostname':row.get('nickname') or row.get('hostname') or row.get('display_name',''),'evidence':'Eero account inventory'})
                capabilities['devices']=True
                capabilities['devices_write']=bool(self.device_urls and self.config.get('write_enabled'))
            except ProviderError as error:errors['devices']=str(error)
        await self.settings_snapshot(result,network,resources)
        self.forward_urls={}
        if resources.get('forwards'):
            try:
                data=await self.request(resources['forwards']);rows=data.get('data',[]) if isinstance(data,dict) else data
                for row in rows if isinstance(rows,list) else []:
                    if not isinstance(row,dict) or not row.get('url'):continue
                    path=self.path(row['url']);fid=path.rsplit('/',1)[-1]
                    # A forwarding object cannot redirect a delete outside this network collection.
                    collection=self.path(resources['forwards']).rstrip('/')
                    if not path.startswith(collection+'/') or '/' in path[len(collection)+1:]:continue
                    self.forward_urls[fid]=path
                    result['forwards'].append({'id':fid,'name':row.get('description',''),'private_ip':row.get('ip'),'protocol':row.get('protocol'),'external_port':row.get('gateway_port'),'internal_port':row.get('client_port'),'enabled':row.get('enabled',True)})
                capabilities['forwards_read']=True;capabilities['forwards_write']=bool(self.config.get('write_enabled'))
            except ProviderError as error:errors['forwards']=str(error)
        result.setdefault('errors',{}).update(errors)
        return result
    async def apply(self,resource,operation,values):
        if resource not in ('forwards','dns_settings','devices','lighting','dhcp','upnp'):raise Unsupported('Eero does not advertise DNS host-record writes')
        snapshot=await self.snapshot()
        if resource in ('devices','lighting','dhcp','upnp'):
            return await self.apply_settings(resource,operation,values,snapshot)
        if resource=='dns_settings':
            if operation!='upsert' or not snapshot['capabilities']['dns_settings_write']:raise Unsupported('Enable Eero upstream DNS writes first')
            value=dns_settings_value(values)
            network=json.loads(json.dumps(self.network))
            network['dns']['mode']=value['mode']
            if value['mode']=='custom':network['dns']['custom']={'ips':value['ips']}
            return await self.request(network['resources']['settings'],'PUT',json=network)
        if not snapshot['capabilities']['forwards_write']:raise Unsupported('Enable Eero writes before managing port forwards')
        if operation=='delete':
            path=self.forward_urls.get(str(values['id']))
            if not path:raise ProviderError('Eero forwarding rule no longer exists')
            return await self.request(path,'DELETE')
        value=forward_value(values)
        path=self.network['resources']['forwards'];method='POST'
        if values.get('id'):
            path=self.forward_urls.get(str(values['id']))
            if not path:raise ProviderError('Forwarding rule no longer exists')
            method='PUT'
        return await self.request(path,method,json={
            'ip':value['private_ip'],'description':value['name'],'protocol':value['protocol'],
            'gateway_port':str(value['external_port']),'client_port':str(value['internal_port']),'enabled':values.get('enabled',True)})

    async def settings_snapshot(self,result,network,resources):
        caps=result['capabilities'];writes=bool(self.config.get('write_enabled'))
        settings=resources.get('settings')
        if isinstance(network.get('upnp'),bool):
            result['upnp']={'enabled':network['upnp']};caps.update(upnp_read=True,upnp_write=bool(writes and settings))
        dhcp=network.get('dhcp')
        if isinstance(dhcp,dict) and dhcp.get('mode') in ('automatic','custom'):
            custom=dhcp.get('custom') or {}
            result['dhcp']={'mode':dhcp['mode'],**{key:custom[key] for key in ('subnet_ip','subnet_mask','start_ip','end_ip') if key in custom}}
            caps.update(dhcp_read=True,dhcp_write=bool(writes and settings))
        self.light_urls={}
        if resources.get('eeros'):
            try:
                rows=await self.request(resources['eeros'])
                if isinstance(rows,dict):rows=rows.get('data',[])
                result['lighting']=[]
                for row in rows if isinstance(rows,list) else []:
                    if not isinstance(row,dict) or not isinstance(row.get('led_on'),bool):continue
                    id=str(row.get('serial') or row.get('url','').rsplit('/',1)[-1])
                    result['lighting'].append({'id':id,'name':row.get('location') or row.get('name') or id,'enabled':row['led_on']})
                    path=(row.get('resources') or {}).get('led_action')
                    if path:self.light_urls[id]=self.path(path)
                caps.update(lighting_read=True,lighting_write=bool(writes and self.light_urls))
            except ProviderError as error:result.setdefault('errors',{})['lighting']=str(error)

    async def apply_settings(self,resource,operation,values,snapshot):
        if not snapshot['capabilities'].get(resource+'_write'):raise Unsupported('This Eero feature is unavailable or writes are disabled')
        if resource=='devices':
            if operation not in ('disconnect','reconnect'):raise Unsupported('Unknown device operation')
            path=self.device_urls.get(str(values['id']))
            if not path:raise ProviderError('Choose an observed device')
            return await self.request(path,'PUT',json={'paused':operation=='disconnect'})
        if resource=='lighting':
            path=self.light_urls.get(str(values['id']))
            if not path:raise ProviderError('Choose an observed Eero node')
            return await self.request(path,'PUT',data={'led_on':'true' if values['enabled'] else 'false'})
        network=json.loads(json.dumps(self.network))
        if resource=='upnp':network['upnp']=values['enabled']
        elif resource=='dhcp':
            if values['mode']=='custom':
                from blueberry_quantum.schema import dhcp_range
                required=('subnet_ip','subnet_mask','start_ip','end_ip')
                if any(not values.get(key) for key in required):raise ProviderError('Custom DHCP requires subnet, mask and lease range')
                dhcp_range(values,values['subnet_ip'],values['subnet_mask'])
                network['dhcp']['custom']={key:values[key] for key in required}
            network['dhcp']['mode']=values['mode']
        return await self.request(network['resources']['settings'],'PUT',json=network)
