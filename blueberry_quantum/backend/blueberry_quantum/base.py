"""Unified provider interfaces and local gateway adapters."""
from __future__ import annotations
import ast
import hashlib
import ipaddress
import json
import re
from urllib.parse import quote, urlsplit
from .inspect import inspect_gateway


class ProviderError(ValueError):
    def __init__(self,message,status=None):
        super().__init__(message)
        self.status=status
class Unsupported(ProviderError): pass


def local_url(value):
    value = value.strip().rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderError('Enter an HTTP or HTTPS provider origin')
    return value


def dns_value(values):
    name = str(values.get('name', '')).strip().lower().rstrip('.')
    if len(name) > 253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name):
        raise ProviderError('Invalid DNS name')
    return {'name': name, 'ip': str(ipaddress.ip_address(values['ip']))}


def forward_value(values):
    ip = ipaddress.IPv4Address(values['private_ip'])
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise ProviderError('Choose a LAN device address')
    protocol = values.get('protocol', 'tcp').lower()
    if protocol not in ('tcp', 'udp', 'both'):
        raise ProviderError('Choose TCP, UDP, or both')
    result = {'name': str(values.get('name', 'Blueberry rule'))[:80], 'private_ip': str(ip), 'protocol': protocol}
    for key in ('external_port', 'internal_port'):
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ProviderError('Ports must be integers from 1 to 65535')
        result[key] = value
    return result


def dns_settings_value(values):
    mode=values.get('mode')
    if mode not in ('automatic','custom'): raise ProviderError('Choose automatic or custom upstream DNS')
    ips=values.get('ips',[])
    if not isinstance(ips,list) or len(ips)>2: raise ProviderError('Choose up to two IPv4 DNS servers')
    result=[]
    for value in ips:
        address=ipaddress.IPv4Address(value)
        if address.is_loopback or address.is_unspecified or address.is_multicast: raise ProviderError('Choose a reachable DNS server address')
        if str(address) not in result: result.append(str(address))
    if mode=='custom' and not result: raise ProviderError('Custom DNS requires a server address')
    return {'mode':mode,'ips':result}


class Provider:
    kind = 'base'
    def __init__(self, session, config):
        self.session, self.config = session, config
        self.url = local_url(config['url'])
    async def text(self, path, method='GET', **kwargs):
        if not path.startswith('/') or path.startswith('//'):
            raise ProviderError('Invalid provider path')
        async with self.session.request(method, self.url + path, allow_redirects=False, timeout=20, **kwargs) as response:
            if response.status in (401, 403): raise ProviderError('Provider authentication is required or expired',status=response.status)
            if response.status >= 300: raise ProviderError(f'Provider returned HTTP {response.status}',status=response.status)
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024: raise ProviderError('Provider response exceeds 8 MiB')
                chunks.append(chunk)
            data = b''.join(chunks)
            return data.decode('utf-8', errors='replace')
    async def json(self, path, method='GET', **kwargs):
        return json.loads(await self.text(path, method, **kwargs))
    @classmethod
    def configuration_schema(cls):
        from .schema import config_schema
        return config_schema()
    @classmethod
    def definition(cls):
        return {'kind':cls.kind,'title':getattr(cls,'title',cls.kind.replace('_',' ').title()),'configuration':cls.configuration_schema()}
    @classmethod
    def validate_configuration(cls,config):return config
    def resource_specs(self,snapshot):return {}
    def actions(self,snapshot):return []
    async def invoke(self,action,values):raise Unsupported('Provider action is not supported')
    async def snapshot(self): raise Unsupported('Provider snapshot is not implemented')
    async def inspect(self): return await inspect_gateway(self.text, self.url)
    async def apply(self, resource, operation, values): raise Unsupported('This provider does not support that write operation')
