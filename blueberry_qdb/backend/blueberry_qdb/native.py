"""State-checked UI relay for the official app. No OAuth or App Check is used."""
import asyncio
import hashlib
import json
import re
import secrets
import time
import xml.etree.ElementTree as ET

from .adb import ADBError, PACKAGE, SERIAL, select_device, ui_dump

STATES={'closed','open','opening','closing','stopped'}
BOUNDS=re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]\Z')
STATE_WORD=re.compile(r'\b(closed|open|opening|closing|stopped)\b',re.I)


class NativeError(ValueError):
    """A stable reason code, not screen contents."""


def text(node):
    return ' '.join(filter(None,(node.get('text','').strip(),node.get('content-desc','').strip())))


def normalized(value):
    return ' '.join(value.casefold().split())


def bounds(node):
    match=BOUNDS.fullmatch(node.get('bounds',''))
    if not match:return None
    x0,y0,x1,y1=map(int,match.groups())
    if not (0<=x0<x1<=10000 and 0<=y0<y1<=10000):return None
    return (x0,y0,x1,y1)


def labels(node):
    return [text(n) for n in node.iter('node') if text(n)]


def door_state(node):
    # Prefer standalone status text. Action labels like 'Open garage door'
    # must not be mistaken for the current state.
    if any(re.search(r'\boffline\b|\bnot connected\b|\bconnection lost\b',normalized(value)) for value in labels(node)):
        return 'unknown'
    entries=[]
    for child in node.iter('node'):
        for value in (child.get('text',''),child.get('content-desc','')):
            value=normalized(value)
            if value=='open' and child.get('clickable')=='true' and not re.search(r'state|status',child.get('resource-id',''),re.I):continue
            state=value if value in STATES else None
            match=re.fullmatch(r'(?:door |garage door )?(?:is )?(closed|open|opening|closing|stopped)',value)
            if match:state=match[1]
            if state and not any(n is child for n,_ in entries):entries.append((child,state))
    leaves=[(n,s) for n,s in entries if not any(other is not n and other in set(n.iter()) for other,_ in entries)]
    if len(leaves)==1:return leaves[0][1]
    return 'unknown'


def signature(node):
    # Labels containing the reported state legitimately change after motion.
    # Normalize just those words; never fall back to absolute screen position.
    return {'resource_id':node.get('resource-id',''),'class':node.get('class',''),
            'label':STATE_WORD.sub('{state}',normalized(text(node)))}


def key_for(selector):
    return hashlib.sha256(json.dumps(selector,sort_keys=True).encode()).hexdigest()[:24]


class Screen:
    def __init__(self,xml):
        try:self.root=ET.fromstring(xml)
        except ET.ParseError:raise NativeError('native_screen_unreadable') from None
        self.parents={c:p for p in self.root.iter() for c in p}

    def card(self,name):
        anchors=[n for n in self.root.iter('node') if n.get('visible-to-user','true')!='false' and normalized(name) in (normalized(n.get('text','')),normalized(n.get('content-desc','')))]
        anchors=[n for n in anchors if not any(other is not n and other in set(n.iter()) for other in anchors)]
        if len(anchors)!=1:
            if any(n.get('password')=='true' or normalized(text(n)) in ('sign in','log in') for n in self.root.iter('node')):
                raise NativeError('native_sign_in_required')
            raise NativeError('native_door_not_unique')
        card=anchors[0]
        while card is not None:
            if door_state(card)!='unknown':return card
            card=self.parents.get(card)
        raise NativeError('native_state_unknown')

    def controls(self,card):
        controls=[]
        for node in card.iter('node'):
            if node.get('clickable')!='true' and node.get('long-clickable')!='true':continue
            if node.get('visible-to-user','true')=='false':continue
            if node.get('enabled','true')!='true' or not bounds(node):continue
            selector=signature(node)
            identity=normalized(text(node)+' '+node.get('resource-id','').replace('_',' '))
            if re.search(r'\b(settings|menu|camera|video|light|lock|account|history)\b',identity):continue
            if door_state(node)!=door_state(card):continue
            # A control must have semantic identity. Anonymous position-only
            # buttons cannot be safely recognized after the UI changes.
            if not selector['resource_id'] and not selector['label']:continue
            controls.append({'key':key_for(selector),'label':text(node) or ' / '.join(labels(node)[:3]) or selector['resource_id'],
                             'selector':selector,'node':node})
        # The status text can be nested below the clickable parent of the card.
        parent=self.parents.get(card)
        if not controls and parent is not None and door_state(parent)==door_state(card):
            return self.controls(parent)
        return controls

    def inspect(self,name):
        card=self.card(name);controls=self.controls(card)
        return {'door_name':name,'door_state':door_state(card),
                'controls':[{'key':c['key'],'label':c['label']} for c in controls]}

    def find_control(self,name,selector):
        card=self.card(name)
        matches=[c for c in self.controls(card) if c['selector']==selector]
        if len(matches)!=1:raise NativeError('native_control_changed')
        return matches[0]['node'],door_state(card)


class NativeController:
    def __init__(self,service,save):
        self.service=service;self.save=save;self.lock=asyncio.Lock()
        self.inspections={}
        self.pending={}

    def bindings(self):
        return self.service.state.setdefault('native_doors',[])

    async def selected(self):
        devices=await self.service.android.discover(self.service.targets())
        return select_device(devices,self.service.options.get('serial') or self.service.state['serial'])

    async def screen(self,serial):
        if not SERIAL.fullmatch(serial) or serial.startswith('-'):raise NativeError('invalid_serial')
        policy=await self.service.android.run('-s',serial,'shell','dumpsys','window','policy')
        if re.search(r'(?:mShowingLockscreen|isStatusBarKeyguard|showing)\s*=\s*true',policy):
            raise NativeError('native_phone_locked')
        activities=await self.service.android.run('-s',serial,'shell','dumpsys','activity','activities')
        resumed=[line for line in activities.splitlines() if 'topResumedActivity=' in line or 'mResumedActivity:' in line]
        if not resumed or not any(PACKAGE+'/' in line for line in resumed):
            raise NativeError('native_open_myq')
        path='/data/local/tmp/blueberryqdb-native.xml'
        try:
            xml=await ui_dump(self.service.android,serial,path)
        except ADBError:
            raise NativeError('native_screen_unreadable') from None
        screen=Screen(xml)
        if any(re.search(r'\boffline\b|\bnot connected\b|\bconnection lost\b',normalized(value)) for value in labels(screen.root)):
            raise NativeError('native_app_offline')
        roots=list(screen.root)
        if any(n.get('package') and n.get('package')!=PACKAGE for n in roots):raise NativeError('native_screen_obstructed')
        if any(n.get('resource-id') in ('android:id/parentPanel','android:id/alertTitle') or
               re.search(r'\bare you sure\b|\bconfirm (?:open|close)\b',normalized(text(n)))
               for n in screen.root.iter('node')):
            raise NativeError('native_confirmation_required')
        return screen

    async def inspect(self,name):
        if not isinstance(name,str) or not 1<=len(name.strip())<=160:raise NativeError('native_door_name_required')
        async with self.lock:
            serial=await self.selected();screen=await self.screen(serial)
            result=screen.inspect(name.strip());card=screen.card(name.strip())
            choices=screen.controls(card)
            inspection=secrets.token_urlsafe(24)
            self.inspections={inspection:{'serial':serial,'name':name.strip(),'choices':choices,'expires_at':time.time()+300}}
            return {**result,'inspection':inspection}

    async def bind(self,inspection,control_key,gesture='tap'):
        async with self.lock:
            record=self.inspections.get(inspection)
            if not record or record['expires_at']<time.time():raise NativeError('native_inspect_again')
            if gesture not in ('tap','hold'):raise NativeError('native_gesture_invalid')
            choices=[c for c in record['choices'] if c['key']==control_key]
            if len(choices)!=1:raise NativeError('native_control_not_unique')
            # Inspect again before saving, but never actuate during calibration.
            serial=await self.selected()
            if serial!=record['serial']:raise NativeError('native_phone_changed')
            screen=await self.screen(serial)
            node,state=screen.find_control(record['name'],choices[0]['selector'])
            if door_state(node)!=state:raise NativeError('native_select_state_toggle')
            existing=next((b for b in self.bindings() if b['name']==record['name'] and b['serial']==serial),None)
            binding={'id':existing['id'] if existing else secrets.token_hex(12),'serial':serial,
                     'name':record['name'],'selector':choices[0]['selector'],'gesture':gesture}
            self.service.state['native_doors']=[b for b in self.bindings() if b['id']!=binding['id']]+[binding]
            self.service.state['serial']=serial
            self.save(self.service.path,self.service.state)
            self.inspections.clear()
            return {'id':binding['id'],'name':binding['name'],'door_state':state}

    async def devices(self):
        async with self.lock:
            if not self.bindings():raise NativeError('native_calibration_required')
            serial=await self.selected();screen=await self.screen(serial)
            result={}
            for binding in self.bindings():
                if binding['serial']!=serial:continue
                key='native/'+self.service.state['id']+'/'+binding['id']
                try:
                    _,state=screen.find_control(binding['name'],binding['selector'])
                    online=state!='unknown'
                except NativeError:state='unknown';online=False
                result[key]={'serial_number':binding['id'],'name':binding['name'],'native_binding_id':binding['id'],
                             'state':{'door_state':state,'online':online}}
            return result

    async def command(self,binding_id,action):
        if action not in ('open','close'):raise NativeError('native_action_invalid')
        deadline=time.monotonic()+15
        async with self.lock:
            if time.monotonic()>deadline:raise NativeError('native_controller_busy')
            binding=next((b for b in self.bindings() if b['id']==binding_id),None)
            if not binding:raise NativeError('native_binding_missing')
            serial=await self.selected()
            if serial!=binding['serial']:raise NativeError('native_phone_changed')
            screen=await self.screen(serial)
            node,state=screen.find_control(binding['name'],binding['selector'])
            desired='open' if action=='open' else 'closed'
            pending=self.pending.get(binding_id)
            if pending and time.monotonic()-pending['at']<10 and state!=pending['desired']:
                raise NativeError('native_command_pending')
            if state==desired:return {'input_sent':False,'reported_state':state}
            if state not in ('open','closed'):raise NativeError('native_door_busy_or_unknown')
            # If a selected widget has an explicit action label, do not press
            # it for the opposite request. A calibrated state-label toggle is
            # permitted only with the verified inverse state above.
            label=normalized(text(node))
            if re.search(r'\bclose\b',label) and action!='close':raise NativeError('native_control_wrong_action')
            if re.search(r'\bopen\b',label) and label not in STATES and action!='open':
                raise NativeError('native_control_wrong_action')
            x0,y0,x1,y1=bounds(node);x,y=(x0+x1)//2,(y0+y1)//2
            if time.monotonic()>deadline:raise NativeError('native_controller_busy')
            if binding['gesture']=='hold':
                await self.service.android.run('-s',serial,'shell','input','swipe',str(x),str(y),str(x),str(y),'1500')
            else:await self.service.android.run('-s',serial,'shell','input','tap',str(x),str(y))
            self.pending[binding_id]={'at':time.monotonic(),'desired':desired}
            await self.screen(serial)  # Detect an app confirmation; never guess its buttons.
            # One gesture only. Confirmations and UI changes are deliberately
            # not guessed or retried, and no optimistic door state is produced.
            return {'input_sent':True,'reported_state':state,'state_verified_after_input':False}
