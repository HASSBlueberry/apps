from urllib.parse import quote
from ..base import Provider,ProviderError,Unsupported,dns_value,forward_value

class ProxyProvider(Provider):
    kind = 'blueberry_proxy'
    title='BlueberryProxy'
    roles=['discovery','dns']
    def resource_specs(self,snapshot):
        from ..schema import obj,field,ID
        note='BlueberryProxy is a DNS and proxy service. Select a gateway provider for router hardware controls.'
        return {**{key:{'reason':note} for key in ('upnp','lighting','forwards','dhcp','dmz')},
            'devices':{'title':'Discovered devices','reason':'Discovery evidence does not establish current connectivity. This provider cannot disconnect clients.'},
            'dns_settings':{'title':'DNS upstreams','collection':True,'allow_create':True,'columns':['name','protocol','host','port'],
                'schema':obj({'id':ID,'name':field('Resolver name',**{'x-immutable':True}),'protocol':field('Transport',enum=['udp','tcp','https'],default='udp'),'host':field('Address or full HTTPS URL'),'port':field('Port','integer',minimum=1,maximum=65535,default=53),'timeout':field('Timeout in milliseconds','integer',minimum=50,maximum=30000,default=3000),'weight':field('Weight','integer',minimum=1,maximum=1000,default=1)},['name','protocol','host']),
                'operations':{'upsert':'Save upstream','delete':'Delete upstream'},'notice':'Existing cached DNS answers retain their TTL.'}}
    async def start_discovery(self,networks):
        return await self.json('/uscan/jobs','POST',json={'parameters':{'cidrs':networks},'scanner_ids':['arp','mdns','wsd','onvif','avahi']})
    async def import_inventory(self,networks,devices,source):
        return await self.json('/uscan/devices','POST',json={'source':source,'cidrs':networks,'devices':devices})
    async def snapshot(self):
        status = await self.json('/status')
        if status.get('name') != 'blueberryProxy': raise ProviderError('This endpoint is not BlueberryProxy')
        rules = (await self.json('/rules')).get('rules', [])
        scan = await self.json('/scan')
        try:
            discovery = await self.json('/uscan?limit=1000')
        except (ProviderError, ValueError):
            discovery = {}
        devices = {row['ip']: row for row in [*scan.get('discoveries', []), *scan.get('arp', [])] if isinstance(row, dict) and row.get('ip')}
        dns = [{'id': str(row['id']), 'name': row.get('match'), 'ip': row.get('value')}
               for row in rules if row.get('action') == 'static' and row.get('match_type') == 'exact']
        upstreams=[];upstream_error=None
        try:
            upstreams=[{'id':str(row['name']),**{key:row[key] for key in ('name','protocol','host','port','timeout','weight') if key in row and row[key] is not None}} for row in (await self.json('/upstreams')).get('upstreams',[])]
        except ProviderError as error:upstream_error=str(error)
        result = {'status': {'model': 'BlueberryProxy', 'uptime_ms': status.get('uptime-ms')},
                'devices': list(devices.values()), 'networks': list(dict.fromkeys(row['cidr'] for row in discovery.get('interfaces', []) if row.get('cidr'))),
                'dns': dns, 'forwards': [], 'capabilities': {'dns_read': True, 'dns_write': bool(self.config.get('write_enabled',True)), 'forwards_read': False, 'forwards_write': False, 'devices': True,'dns_settings_read':upstream_error is None,'dns_settings_write':upstream_error is None and bool(self.config.get('write_enabled',True))}}
        result['dns_settings']=upstreams
        if upstream_error:result['errors']={'dns_settings':upstream_error}
        for row in result['devices']:row['id']=str(row.get('mac') or row['ip'])
        return result
    async def apply(self, resource, operation, values):
        if resource=='dns_settings':
            if not self.config.get('write_enabled',True):raise Unsupported('Enable supported writes first')
            if operation=='delete':return await self.json('/upstreams','DELETE',json={'name':values['id']})
            if values.get('id') and values['id']!=values['name']:raise ProviderError('Resolver names cannot be changed in place')
            if values['protocol']=='https':
                from urllib.parse import urlsplit
                if urlsplit(values['host']).scheme!='https' or not urlsplit(values['host']).hostname:raise ProviderError('DNS-over-HTTPS requires a full HTTPS URL')
            return await self.json('/upstreams','POST',json={key:value for key,value in values.items() if key!='id'})
        if resource != 'dns': raise Unsupported('BlueberryProxy is not a WAN NAT gateway')
        if operation == 'delete':
            return await self.json('/rules?id=' + quote(values['id'], safe=''), 'DELETE')
        value = dns_value(values)
        rows = (await self.snapshot())['dns']
        current = next((row for row in rows if (row['id']==str(values['id']) if values.get('id') else row['name']==value['name'])), None)
        return await self.json('/rules', 'POST', json={'id': current['id'] if current else 'quantum-' + value['name'],
                          'match_type': 'exact', 'match': value['name'], 'action': 'static', 'value': value['ip']})

