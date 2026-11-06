from urllib.parse import quote
from ..base import Provider,ProviderError,Unsupported,dns_value,forward_value

class JsonProvider(Provider):
    """Adapter for external providers implementing the Quantum v1 resource contract."""
    kind = 'http_json'
    title='External JSON provider'
    @classmethod
    def configuration_schema(cls):
        from ..schema import config_schema,field
        return config_schema({'token':field('API bearer token',writeOnly=True)})
    def resource_specs(self,snapshot):
        return snapshot.get('_schema',{}).get('resources',{})
    async def snapshot(self):
        snapshot=await self.json('/v1/snapshot', headers=self.headers())
        try:
            snapshot['_schema']=await self.json('/v1/schema',headers=self.headers())
        except ProviderError as error:
            if error.status!=404:raise
        if not self.config.get('write_enabled',False):
            for key in list(snapshot.get('capabilities',{})):
                if key.endswith('_write'):snapshot['capabilities'][key]=False
            for resource in snapshot.get('_schema',{}).get('resources',{}).values():resource['write']=False
        return snapshot
    def headers(self):
        return {'Authorization':'Bearer ' + self.config['token']} if self.config.get('token') else {}
    async def apply(self, resource, operation, values):
        if operation not in ('upsert','delete'):
            return await self.json('/v1/'+quote(resource,safe='')+'/'+quote(operation,safe=''),'POST',headers=self.headers(),json=values)
        return await self.json('/v1/' + resource + ('/' + quote(values['id'], safe='') if operation == 'delete' else ''),
                               'DELETE' if operation == 'delete' else 'POST', headers=self.headers(),
                               **({'json':values} if operation != 'delete' else {}))

