import asyncio
import hashlib
import ipaddress
import json
import re
from ..parsing import call_literals,arc_digest
from ..base import Provider,ProviderError,Unsupported,dns_value,forward_value

class G3100Provider(Provider):
    """Negotiated CGI profile. Exact G3100 firmware compatibility remains under RE."""
    kind = 'quantum_g3100'
    title='Quantum G3100 Gateway'
    @classmethod
    def configuration_schema(cls):
        from ..schema import config_schema,field
        return config_schema({'username':field('Username',default='admin'),'password':field('Administration password',writeOnly=True)},{'url':'https://192.168.1.1'})
    def __init__(self, session, config):
        super().__init__(session, config)
        self.logged_in = False
        self.report = None
        self.inspection_attempted = False
        self.inspection_error = None
        self.auth_attempted = False
        self.dns_cfg = {}
        self.gateway_cfg = {}
        self.raw_forwards = {}
    async def login(self):
        if self.auth_attempted:
            raise ProviderError('Sign in again explicitly to retry gateway authentication')
        self.auth_attempted = True
        challenge = await self.json('/loginStatus.cgi')
        token = challenge.get('loginToken') if isinstance(challenge, dict) else None
        if not isinstance(token, str) or not re.fullmatch('[a-fA-F0-9]{32}', token):
            raise Unsupported('Gateway does not expose the inspected CGI challenge profile. Run SPA inspection for this firmware.')
        password = self.config.get('password', '')
        if not password: raise ProviderError('Enter the gateway administration password')
        password_hash = hashlib.sha512((token + arc_digest(password)).encode()).hexdigest()
        async with self.session.post(self.url + '/login.cgi', data={
            'luci_username': arc_digest(self.config.get('username', 'admin')),
            'luci_password': password_hash, 'luci_token': token, 'luci_view': 'Desktop', 'luci_keep_login': '0',
        }, headers={'Referer': self.url + '/'}, allow_redirects=False, timeout=20) as response:
            if response.status not in (200, 302, 303): raise ProviderError(f'Gateway login returned HTTP {response.status}')
            await response.read()
        text = await self.text('/cgi/cgi_status.js')
        if not list(call_literals(text, 'addROD')) and not list(call_literals(text, 'addCfg')):
            raise ProviderError('Gateway did not grant a usable administration session')
        self.logged_in = True
    async def inspect(self):
        self.inspection_attempted = True
        self.inspection_error = None
        self.report = await super().inspect()
        return self.report
    async def snapshot(self):
        if self.config.get('write_enabled') and self.report is None and not self.inspection_attempted:
            self.inspection_attempted=True
            try:await asyncio.wait_for(self.inspect(),timeout=25)
            except (ProviderError,TimeoutError,OSError):self.inspection_error='Could not verify this firmware. Use Inspect provider to retry.'
        if not self.logged_in: await self.login()
        self.connection_cfg = {}
        status_text = await self.text('/cgi/cgi_status.js')
        rod = {row[0]: row[1] for row in call_literals(status_text, 'addROD') if len(row) == 2}
        cfg = {row[0]: {'field':row[1], 'value':row[2]} for row in call_literals(status_text, 'addCfg') if len(row) >= 3}
        if not rod and not cfg:
            self.logged_in = False
            raise ProviderError('Gateway session expired; sign in again')
        dns_rows, forwards, devices, errors = [], [], [], {}
        try:
            text = await self.text('/cgi/cgi_dns_server.js')
            self.dns_cfg = {row[0]: {'field':row[1], 'value':row[2]} for row in call_literals(text, 'addCfg') if len(row) >= 3}
            entries = {key:row['value'] for key,row in self.dns_cfg.items()}
            for key, value in entries.items():
                match = re.fullmatch('dns_name(\\d+)', key)
                if match and value:
                    dns_rows.append({'id': match[1], 'name': value, 'ip': entries.get('dns_ip' + match[1], '')})
        except ProviderError as error: errors['dns'] = str(error)
        try:
            text = await self.text('/cgi/cgi_firewall_port_forward.js')
            values = {row[0]: row[1] for row in call_literals(text, 'addROD') if len(row) == 2}
            raw = values.get('portforwardings', {})
            raw_rules=values.get('portrules',{})
            rule_rows=raw_rules.get('portrules',[]) if isinstance(raw_rules,dict) else []
            self.port_rules={str(row.get('id')):row for row in rule_rows if isinstance(row,dict)}
            forwards = [row for row in raw.get('portforwardings', []) if isinstance(row, dict)] if isinstance(raw, dict) else []
            for row in forwards: row['id'] = str(row.get('id', ''))
        except ProviderError as error: errors['forwards'] = str(error)
        try:
            text = await self.text('/cgi/cgi_owl.js')
            values = {row[0]: row[1] for row in call_literals(text, 'addROD') if len(row) == 2}
            known = values.get('known_device_list', [])
            if isinstance(known, dict): known = known.get('known_devices', [])
            for row in known if isinstance(known, list) else []:
                if not isinstance(row, dict): continue
                if 'activity' in row and str(row['activity']) != '1': continue
                ip = row.get('ip') or row.get('ipAddress') or row.get('ipaddr')
                mac = row.get('mac') or row.get('macAddress') or row.get('macaddr')
                try:
                    address = ipaddress.IPv4Address(ip)
                    if address.is_unspecified or address.is_multicast or address.is_loopback: continue
                except (ValueError,TypeError): continue
                devices.append({'ip': str(address), 'mac': mac, 'hostname': row.get('name', row.get('hostname', ''))})
        except ProviderError as error: errors['devices'] = str(error)
        writes = bool(self.config.get('write_enabled'))
        evidence = self.report or {}
        # Port writes are gated by an observed matching form marker and explicit configuration.
        forwards_write = writes and evidence.get('profiles', {}).get('forwardrule', False) and not errors.get('forwards')
        networks = list(self.config.get('networks', []))
        try:
            text = await self.text('/cgi/cgi_net_connections.js')
            self.connection_cfg = {row[0]:{'field':row[1],'value':row[2]} for row in call_literals(text,'addCfg') if len(row)>=3}
            network_cfg = {key:row['value'] for key,row in self.connection_cfg.items()}
            if network_cfg.get('lan_ip') and network_cfg.get('lan_mask0'):
                network = str(ipaddress.IPv4Network(network_cfg['lan_ip']+'/'+network_cfg['lan_mask0'],strict=False))
                if network not in networks: networks.append(network)
        except (ProviderError,ValueError):
            errors['networks'] = 'LAN CIDRs unavailable. Enter explicit provider networks for discovery.'
        result = {'status': {'model': rod.get('model', rod.get('model_name', 'G3100 CGI profile')),
                           'firmware': rod.get('firmware_version', ''), 'uptime': rod.get('uptime'), 'wan_ip': rod.get('get_wan4_ip')},
                'devices': devices, 'networks': networks, 'dns': dns_rows, 'forwards': forwards,
                'capabilities': {'devices': not bool(errors.get('devices')), 'dns_read': not bool(errors.get('dns')),
                                 'dns_write': writes and evidence.get('profiles', {}).get('dns', False) and bool(self.dns_cfg), 'forwards_read': not bool(errors.get('forwards')), 'forwards_write': forwards_write},
                'errors': {**errors,**({'inspection':self.inspection_error} if self.inspection_error else {})}, 'profile': 'luci_cgi_experimental', 'inspection': evidence}
        await self.gateway_settings(result)
        return result

    def resource_specs(self,snapshot):
        from ..schema import obj,field,ENABLED,DHCP,UPSTREAM
        from copy import deepcopy
        dhcp=deepcopy(DHCP);dhcp['properties'].update(subnet_ip=field('LAN subnet address',format='ipv4',readOnly=True),subnet_mask=field('LAN subnet mask',format='ipv4',readOnly=True))
        dns=deepcopy(UPSTREAM);dns['properties']['mode']['enum'].append('disabled')
        return {'lighting':{'schema':obj({'enabled':ENABLED,'brightness':field('Brightness percent','integer',enum=[0,50,100]),'timeout':field('LED timeout in minutes','integer',enum=list(dict.fromkeys([0,1,5,10,15,20,30]+([snapshot['lighting']['timeout']] if snapshot.get('lighting',{}).get('timeout') is not None else []))))},['enabled'])},
                'dhcp':{'title':'LAN DHCP','schema':dhcp},'dns_settings':{'schema':dns}}

    async def gateway_settings(self,result):
        self.gateway_cfg=dict(self.connection_cfg)
        names={'upnp':'upnp','lighting':'router_led','dhcp':'ip_distribution','dmz':'dmz','devices':'firewall_access_control'}
        for resource,cgi in names.items():
            try:
                text=await self.text('/cgi/cgi_'+cgi+'.js')
                fields={row[0]:{'field':row[1],'value':row[2]} for row in call_literals(text,'addCfg') if len(row)>=3}
                if not fields:raise Unsupported('Firmware did not advertise settings fields')
                self.gateway_cfg.update(fields)
            except ProviderError as error:
                result['errors'][resource]=str(error)
        cfg=self.gateway_cfg
        value=lambda key,default=None:cfg.get(key,{}).get('value',default)
        specs={'upnp':['upnp_enable'],'lighting':['router_led_dim'],'dhcp':['dhcp_enable0','dhcp_start_ip','dhcp_end_ip','dhcp_lease_time'],'dmz':['firewall_dmz_enable','firewall_dmz_ip'],'dns_settings':['wan_dns_enable','dns_auto_enable','wan_static_dns']}
        markers={'upnp':'upnp','lighting':'lighting','dhcp':'dhcp','dmz':'dmz','devices':'device_access','dns_settings':'upstream_dns'}
        profiles=(self.report or {}).get('profiles',{})
        for resource,fields in specs.items():
            readable=all(key in cfg for key in fields) and resource not in result['errors']
            result['capabilities'][resource+'_read']=readable
            result['capabilities'][resource+'_write']=bool(readable and self.config.get('write_enabled') and profiles.get(markers[resource]))
        if result['capabilities']['upnp_read']:result['upnp']={'enabled':str(value('upnp_enable'))=='1'}
        if result['capabilities']['lighting_read']:
            brightness={'on':100,'dim':50,'off':0}.get(value('router_led_dim'))
            result['lighting']={'enabled':brightness!=0,'brightness':brightness}
            if str(value('router_led_timer','')).isdigit():result['lighting']['timeout']=int(value('router_led_timer'))
        if result['capabilities']['dhcp_read']:
            result['dhcp']={'enabled':str(value('dhcp_enable0'))=='1','start_ip':value('dhcp_start_ip'),'end_ip':value('dhcp_end_ip'),'lease_minutes':int(value('dhcp_lease_time'))}
            result['dhcp'].update(subnet_ip=value('lan_ip'),subnet_mask=value('lan_mask',value('lan_mask0')))
        if result['capabilities']['dns_settings_read']:
            mode='disabled' if str(value('wan_dns_enable'))!='1' else 'automatic' if str(value('dns_auto_enable'))=='1' else 'custom'
            ips=[ip for ip in str(value('wan_static_dns','')).split('|') if ip]
            result['dns_settings']={'mode':mode,'ips':ips}
        if result['capabilities']['dmz_read']:result['dmz']={'enabled':str(value('firewall_dmz_enable'))=='1','ip':value('firewall_dmz_ip')}
        blocked={str(row['value']).lower():key for key,row in cfg.items() if key.startswith('block_mac_') and row['value']}
        slots=[key for key in cfg if key.startswith('block_mac_')]
        result['capabilities']['devices_write']=bool(slots and self.config.get('write_enabled') and profiles.get('device_access'))
        for row in result['devices']:
            row['id']=str(row.get('mac') or row['ip']).lower()
            row['connected']=True;row['blocked']=str(row.get('mac','')).lower() in blocked
        self.raw_forwards={str(row['id']):dict(row) for row in result['forwards']}
        # Keep firmware-specific keys for backward compatibility and expose normalized columns.
        for row in result['forwards']:
            row['enabled']=str(row.get('enable','1'))=='1'
            row['private_ip']=row.get('privateIP',row.get('private_ip'))
            rule=self.port_rules.get(str(row.get('port_rule_id')),{})
            ports=rule.get('ports',[])
            port=ports[0] if ports else {}
            external=port.get('dest_port',row.get('external_port'))
            row['external_port']=int(external) if str(external).isdigit() else external
            row['protocol']={1:'tcp',2:'udp',8:'both'}.get(int(port.get('type',0)),row.get('protocol'))
            internal=row.get('forward_port',row.get('internal_port'))
            row['internal_port']=row['external_port'] if str(internal)=='0' else int(internal) if str(internal).isdigit() else internal

    async def submit_fields(self,changes,command=None,parameter=None):
        token=(await self.json('/loginStatus.cgi')).get('token')
        if not token:raise ProviderError('Gateway apply token is missing')
        form={'token':token}
        if command:form['action']=command
        if parameter is not None:form['action_params']=str(parameter)
        for key,value in changes.items():
            field=self.gateway_cfg.get(key,{}).get('field')
            if not isinstance(field,(str,int)):raise Unsupported('Firmware did not advertise '+key)
            form[str(field)]=str(value)
        await self.text('/apply_abstract.cgi','POST',data=form)
        return {'submitted':True}

    async def apply_gateway(self,resource,operation,values,snapshot):
        if not snapshot['capabilities'].get(resource+'_write'):raise Unsupported('Inspect this firmware and enable supported writes first')
        if resource=='dns_settings':
            mode=values['mode'];ips=values.get('ips',[])
            if mode=='custom':
                from ..base import dns_settings_value
                ips=dns_settings_value(values)['ips']
            return await self.submit_fields({'wan_dns_enable':0 if mode=='disabled' else 1,'dns_auto_enable':1 if mode=='automatic' else 0,'wan_static_dns':'|'.join(ips) if mode=='custom' else ''},'ui_verizon_broad_settings')
        if resource=='upnp':return await self.submit_fields({'upnp_enable':int(values['enabled'])},'ui_upnp')
        if resource=='lighting':
            changes={'router_led_dim':'off' if not values['enabled'] else {50:'dim',100:'on'}.get(values.get('brightness'),'on')}
            if 'timeout' in values:changes['router_led_timer']=values['timeout']
            return await self.submit_fields(changes)
        if resource=='dmz':
            address=values.get('ip','')
            if values['enabled']:
                ip=ipaddress.IPv4Address(address)
                if not any(ip in ipaddress.IPv4Network(cidr) for cidr in snapshot['networks']):raise ProviderError('DMZ host must be in an advertised LAN network')
            return await self.submit_fields({'firewall_dmz_enable':int(values['enabled']),'firewall_dmz_ip':address if values['enabled'] else ''},'ui_firewall')
        if resource=='dhcp':
            from ..schema import dhcp_range
            cfg=snapshot['dhcp'];dhcp_range(values,cfg['subnet_ip'],cfg['subnet_mask'])
            return await self.submit_fields({'dhcp_enable0':int(values['enabled']),'dhcp_start_ip':values['start_ip'],'dhcp_end_ip':values['end_ip'],'dhcp_lease_time':values['lease_minutes']})
        if resource=='devices':
            row=next((row for row in snapshot['devices'] if row['id']==values['id']),None)
            if row is None or not row.get('mac'):raise ProviderError('Choose an observed device MAC address')
            mac=str(row['mac']).upper().replace('-',':')
            if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}',mac):raise ProviderError('Observed MAC address is invalid')
            slots={key.rsplit('_',1)[-1]:item['value'] for key,item in self.gateway_cfg.items() if key.startswith('block_mac_')}
            matching=next((slot for slot,value in slots.items() if str(value).upper()==mac),None)
            if operation=='reconnect':
                if matching is None:return {'submitted':False,'already_allowed':True}
                return await self.submit_fields({},'ui_remove_block_dev',matching)
            if operation!='disconnect':raise Unsupported('Unknown device access operation')
            if matching is not None:return {'submitted':False,'already_blocked':True}
            free=next((slot for slot,value in slots.items() if not value),None)
            if free is None:raise ProviderError('Gateway device block list is full')
            return await self.submit_fields({},'ui_block_dev',mac+','+free)
        raise Unsupported('Unknown gateway feature')
    async def apply(self, resource, operation, values):
        snapshot = await self.snapshot()
        if resource in ('upnp','lighting','dhcp','dmz','devices','dns_settings'):
            return await self.apply_gateway(resource,operation,values,snapshot)
        if resource == 'dns':
            return await self.apply_dns(operation, values, snapshot)
        if resource != 'forwards': raise Unsupported('Unknown gateway resource')
        if not snapshot['capabilities']['forwards_write']: raise Unsupported('Enable the inspected CGI write profile after verifying this gateway firmware')
        status = await self.json('/loginStatus.cgi')
        token = status.get('token')
        if not isinstance(token, str) or not token: raise ProviderError('Gateway did not provide an apply token')
        if operation == 'delete':
            body = {'type': 'delete', 'id': values['id']}
        else:
            value = forward_value(values)
            body = {'type': 'create', 'enable': '1' if values.get('enabled',True) else '0', 'name': value['name'], 'privateIP': value['private_ip'],
                    'forward_port': str(value['internal_port']), 'schedule_rule_id': '0',
                    'ports': [{'type': {'tcp':1,'udp':2,'both':8}[value['protocol']], 'source_type':0,
                               'source_port':'', 'dest_type':1, 'dest_port':str(value['external_port'])}]}
        if operation!='delete' and values.get('id'):
            old=self.raw_forwards.get(str(values['id']))
            if old is None:raise ProviderError('Forwarding rule no longer exists')
            body.update(type='update',id=old['id'],port_rule_id=old.get('port_rule_id',0))
        return {'response': await self.text('/db.cgi', 'POST', data={'token':token, 'data':json.dumps({'type':'edit','to':'forwardrule','body':[body]})})}


    async def apply_dns(self, operation, values, snapshot):
        if not snapshot['capabilities']['dns_write']:
            raise Unsupported('Enable writes and inspect the matching gateway DNS profile first')
        if operation == 'delete':
            existing = next((row for row in snapshot['dns'] if row['id'] == values['id']), None)
            if not existing: raise ProviderError('DNS entry no longer exists')
            index, name, address = existing['id'], '', ''
        else:
            value = dns_value(values)
            if ipaddress.ip_address(value['ip']).version != 4:
                raise Unsupported('This write profile supports IPv4 DNS entries')
            existing = next((row for row in snapshot['dns'] if (row['id']==str(values['id']) if values.get('id') else row['name']==value['name'])), None)
            index = existing['id'] if existing else next((key[8:] for key,row in self.dns_cfg.items() if key.startswith('dns_name') and not row['value']), None)
            if index is None: raise ProviderError('No unused DNS slot was advertised by the gateway')
            name, address = value['name'], value['ip']
        status = await self.json('/loginStatus.cgi')
        if not status.get('token'): raise ProviderError('Gateway apply token is missing')
        form = {'token':status['token'], 'action':'dnsmasq_reload'}
        for key,value in [('dns_name'+index,name), ('dns_ip'+index,address)]:
            field = self.dns_cfg.get(key, {}).get('field')
            if not isinstance(field, (str,int)): raise ProviderError('Gateway DNS field mapping is incomplete')
            form[str(field)] = value
        await self.text('/apply_abstract.cgi', 'POST', data=form)
        return {'submitted':True, 'id':index}

