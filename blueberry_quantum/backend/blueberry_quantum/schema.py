"""Declarative provider interfaces shared by the API and both frontends.

Supports a bounded subset of JSON Schema. Server-side validation is authoritative.
"""
from copy import deepcopy
import ipaddress
import json
import re

def field(title,type='string',**kwargs): return {'type':type,'title':title,**kwargs}
def obj(properties,required=()): return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}
ID=field('ID',maxLength=128)
ENABLED=field('Enabled','boolean')
IP=field('IPv4 address',format='ipv4')
COMMON_CONFIG={
 'name':field('Provider name',maxLength=120), 'url':field('API URL',format='url'),
 'verify_tls':field('Verify HTTPS certificates','boolean',default=True),
 'write_enabled':field('Enable supported writes','boolean',default=False),
 'networks':field('LAN CIDRs','array',items=field('CIDR',format='ipv4-network'),default=[],maxItems=32),
}
DNS=obj({'name':field('DNS name',format='hostname'),'ip':field('Address',format='ip')},['name','ip'])
FORWARD=obj({'id':ID,'name':field('Rule name',default='Blueberry rule',maxLength=80),
 'private_ip':IP,'protocol':field('Protocol',enum=['tcp','udp','both'],default='tcp'),
 'external_port':field('Gateway port','integer',minimum=1,maximum=65535),
 'internal_port':field('Device port','integer',minimum=1,maximum=65535),'enabled':field('Enabled','boolean',default=True)},['private_ip','protocol','external_port','internal_port'])
UPSTREAM=obj({'mode':field('Mode',enum=['automatic','custom']),'ips':field('IPv4 DNS servers','array',items=IP,maxItems=2)},['mode','ips'])
DHCP=obj({'enabled':ENABLED,'start_ip':IP,'end_ip':IP,'lease_minutes':field('Lease time in minutes','integer',minimum=1,maximum=10080)},['enabled','start_ip','end_ip','lease_minutes'])
DMZ=obj({'enabled':ENABLED,'ip':IP},['enabled'])
LIGHTING=obj({'id':ID,'enabled':ENABLED},['enabled'])
CATALOG={
 'devices':{'title':'Connected devices','collection':True,'schema':obj({'id':ID},['id']),'columns':['hostname','ip','mac','connected','blocked'],
            'operations':{'disconnect':'Block network access','reconnect':'Restore network access'},'notice':'Blocking access does not physically disconnect a wired cable or Wi-Fi association.'},
 'upnp':{'title':'UPnP','schema':obj({'enabled':ENABLED},['enabled'])},
 'lighting':{'title':'Router lighting','schema':LIGHTING},
 'forwards':{'title':'Port forwarding','collection':True,'schema':FORWARD,'columns':['name','private_ip','protocol','external_port','internal_port','enabled'],'operations':{'upsert':'Save rule','delete':'Delete rule'}},
 'dhcp':{'title':'DHCP','schema':DHCP,'notice':'DHCP changes can interrupt clients or change their addresses.'},
 'dmz':{'title':'DMZ','schema':DMZ,'notice':'Enabling DMZ exposes the selected device to unsolicited inbound traffic.'},
 'dns':{'title':'DNS host records','collection':True,'schema':DNS,'columns':['name','ip'],'operations':{'upsert':'Save record','delete':'Delete record'}},
 'dns_settings':{'title':'Upstream DNS','schema':UPSTREAM,'notice':'Changing upstream DNS may reboot the gateway or interrupt connected clients.'},
}

def validate(value,schema,partial=False,depth=0):
    if depth>10: raise ValueError('Schema nesting exceeds the supported limit')
    type=schema.get('type','string')
    if type=='object':
        if not isinstance(value,dict): raise ValueError('Expected an object')
        props=schema.get('properties',{})
        if len(props)>128 or len(value)>128: raise ValueError('Too many settings fields')
        unknown=set(value)-set(props)
        if unknown: raise ValueError('Unknown settings fields '+', '.join(sorted(unknown)))
        result={}
        for key,prop in props.items():
            if key in value: result[key]=validate(value[key],prop,depth=depth+1)
            elif not partial and 'default' in prop: result[key]=deepcopy(prop['default'])
            elif not partial and key in schema.get('required',[]): raise ValueError('Required setting '+key)
        return result
    if type=='boolean':
        if not isinstance(value,bool): raise ValueError('Expected boolean for '+schema.get('title','setting'))
    elif type in ('number','integer'):
        if isinstance(value,bool) or not isinstance(value,int if type=='integer' else (int,float)): raise ValueError('Expected '+type)
        if value<schema.get('minimum',float('-inf')) or value>schema.get('maximum',float('inf')): raise ValueError('Number outside setting limits')
    elif type=='array':
        if not isinstance(value,list) or len(value)>min(schema.get('maxItems',256),256) or len(value)<schema.get('minItems',0):raise ValueError('Invalid setting array length')
        value=[validate(item,schema.get('items',{}),depth=depth+1) for item in value]
    elif type=='string':
        if not isinstance(value,str) or len(value)>min(schema.get('maxLength',4096),65536) or len(value)<schema.get('minLength',0):raise ValueError('Invalid text setting')
        format=schema.get('format')
        if format in ('ipv4','ip'): value=str(ipaddress.IPv4Address(value) if format=='ipv4' else ipaddress.ip_address(value))
        if format=='ipv4-network': value=str(ipaddress.IPv4Network(value,strict=False))
        if format=='url':
            from .base import local_url
            value=local_url(value)
        if format=='hostname':
            value=value.lower().rstrip('.')
            if len(value)>253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',value):raise ValueError('Invalid DNS name')
    else:raise ValueError('Unsupported setting type')
    if 'enum' in schema and value not in schema['enum']:raise ValueError('Choose an advertised setting value')
    if 'const' in schema and value!=schema['const']:raise ValueError('Setting value is fixed by this provider')
    return value

def public_values(value,schema):
    if not isinstance(value,dict):return value
    return {key:public_values(item,schema.get('properties',{}).get(key,{})) for key,item in value.items() if not schema.get('properties',{}).get(key,{}).get('writeOnly')}

def interfaces(provider,snapshot):
    caps=snapshot.get('capabilities',{})
    overrides=provider.resource_specs(snapshot) if hasattr(provider,'resource_specs') else {}
    result=[]
    for rid in dict.fromkeys([*CATALOG,*overrides]):
        item=deepcopy(CATALOG.get(rid,{'title':rid,'schema':obj({})}))
        item.update(deepcopy(overrides.get(rid,{})))
        read=bool(caps.get(rid+'_read',caps.get('devices',False) if rid=='devices' else False))
        write=bool(caps.get(rid+'_write',False))
        item.update(id=rid,read=read,write=write,value=snapshot.get(rid,[] if item.get('collection') else {}))
        item.setdefault('operations',{'upsert':'Save settings'})
        if not read:item['reason']=snapshot.get('errors',{}).get(rid) or item.get('reason') or 'This provider or firmware does not expose this feature.'
        elif not write:item['reason']=item.get('reason') or 'Read-only. Enable supported writes in provider settings.'
        result.append(item)
    return result

def config_schema(extra=None,defaults=None):
    props=deepcopy(COMMON_CONFIG);props.update(deepcopy(extra or {}))
    for key,value in (defaults or {}).items():props[key]['default']=value
    return obj(props,['url'])

def dhcp_range(values,subnet,mask):
    network=ipaddress.IPv4Network(subnet+'/'+mask,strict=False)
    start,end=ipaddress.IPv4Address(values['start_ip']),ipaddress.IPv4Address(values['end_ip'])
    if start not in network or end not in network or int(start)>int(end) or start==network.network_address or end==network.broadcast_address:raise ValueError('DHCP range must contain usable addresses in the LAN subnet')
