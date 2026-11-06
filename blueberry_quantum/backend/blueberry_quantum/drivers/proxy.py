from urllib.parse import quote
from ..base import Provider,ProviderError,Unsupported,dns_value,forward_value

class ProxyProvider(Provider):
    kind = 'blueberry_proxy'
    title='BlueberryProxy'
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
        return {'status': {'model': 'BlueberryProxy', 'uptime_ms': status.get('uptime-ms')},
                'devices': list(devices.values()), 'networks': list(dict.fromkeys(row['cidr'] for row in discovery.get('interfaces', []) if row.get('cidr'))),
                'dns': dns, 'forwards': [], 'capabilities': {'dns_read': True, 'dns_write': True, 'forwards_read': False, 'forwards_write': False, 'devices': True}}
    async def apply(self, resource, operation, values):
        if resource != 'dns': raise Unsupported('BlueberryProxy is not a WAN NAT gateway')
        if operation == 'delete':
            return await self.json('/rules?id=' + quote(values['id'], safe=''), 'DELETE')
        value = dns_value(values)
        rows = (await self.snapshot())['dns']
        current = next((row for row in rows if row['name'] == value['name']), None)
        return await self.json('/rules', 'POST', json={'id': current['id'] if current else 'quantum-' + value['name'],
                          'match_type': 'exact', 'match': value['name'], 'action': 'static', 'value': value['ip']})

