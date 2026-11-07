"""Bounded sign-in assistance on an authorized phone, using visible UI fields."""
import asyncio
import re
import shlex
import time
import xml.etree.ElementTree as ET
from .adb import ADBError, PACKAGE, select_device, ui_dump
from .mailbox import MailboxError, VerificationMailbox


class RecoveryError(ValueError):pass


class LoginRecovery:
    def __init__(self,service):
        self.service=service
        self.task=None
        self.phase='idle'
        self.error=None
        self.started_at=None

    def summary(self):
        return {'phase':self.phase,'error':self.error,'started_at':self.started_at,
                'running':bool(self.task and not self.task.done())}

    async def start(self):
        if self.task and not self.task.done():return
        if not self.service.options.get('login_recovery_enabled'):raise RecoveryError('login_recovery_not_configured')
        if self.service.logins.summary().get('status') in ('waiting','ready'):raise RecoveryError('phone_login_in_progress')
        self.task=asyncio.create_task(self.run())

    async def run(self):
        try:
            async with asyncio.timeout(180):
                await self._run()
        except TimeoutError:
            self.phase='failed';self.error='login_recovery_timeout'

    async def _run(self):
        service=self.service
        self.error=None;self.started_at=time.time();self.phase='preparing'
        try:
            async with service.native.lock:
                options=service.options
                if not options.get('myq_email') or not options.get('myq_password'):raise RecoveryError('myq_credentials_required')
                mailbox=VerificationMailbox(options)
                await asyncio.to_thread(mailbox.read,self.started_at,True)
                serial=select_device(await service.android.discover(service.targets()),options.get('serial') or service.state['serial'])
                await service.android.open_myq(serial)
                deadline=time.monotonic()+180
                password_sent=False;code_sent=False;actions=set()
                while time.monotonic()<deadline:
                    await service.scan()
                    cache=service.state['cache']
                    if cache.get('serial')==serial and cache.get('expires_at',0)>time.time()+300:
                        self.phase='token_available';return
                    root=await self.screen(serial)
                    nodes=list(root.iter('node'))
                    edits=[n for n in nodes if n.get('class','').endswith('EditText') and n.get('enabled')=='true']
                    passwords=[n for n in edits if n.get('password')=='true']
                    if passwords and not password_sent:
                        email_fields=[n for n in edits if n not in passwords]
                        if len(passwords)!=1 or len(email_fields)!=1:raise RecoveryError('login_ui_not_recognized')
                        self.phase='entering_credentials'
                        await self.fill(serial,email_fields[0],options['myq_email'])
                        current=list((await self.screen(serial)).iter('node'))
                        passwords=[n for n in current if n.get('class','').endswith('EditText') and n.get('password')=='true']
                        if len(passwords)!=1:raise RecoveryError('login_ui_not_recognized')
                        await self.fill(serial,passwords[0],options['myq_password'])
                        current=list((await self.screen(serial)).iter('node'))
                        await self.button(serial,current,('log in','login','sign in'))
                        password_sent=True
                    elif not passwords and len(edits)==1 and any('verification' in (n.get('text','')+' '+n.get('content-desc','')).lower() or 'code' in n.get('resource-id','').lower() for n in nodes):
                        self.phase='waiting_for_email'
                        if not code_sent:
                            for label in ('use email','send code via email','send email','email me a code'):
                                if label not in actions and await self.button(serial,nodes,(label,),required=False):
                                    actions.add(label)
                                    break
                            else:
                                label=None
                            if label:
                                await asyncio.sleep(3)
                                continue
                            code=await asyncio.to_thread(mailbox.read,self.started_at)
                            if code:
                                await self.fill(serial,edits[0],code)
                                current=list((await self.screen(serial)).iter('node'))
                                await self.button(serial,current,('verify','submit','continue','verify code'))
                                code_sent=True;self.phase='awaiting_app_check'
                    else:
                        for label in ('log in','login','sign in','email','send code','send verification code','use email'):
                            if label in actions:continue
                            if await self.button(serial,nodes,(label,),required=False):
                                actions.add(label);break
                        self.phase='awaiting_app_check' if password_sent else 'reading_login_screen'
                    await asyncio.sleep(3)
                raise RecoveryError('login_recovery_timeout')
        except asyncio.CancelledError:
            self.phase='cancelled';raise
        except (RecoveryError,MailboxError,ADBError,OSError,ET.ParseError) as error:
            self.phase='failed';self.error=str(error) if isinstance(error,(RecoveryError,MailboxError,ADBError)) else 'login_recovery_failed'
        finally:
            if self.phase not in ('token_available','cancelled','failed'):
                self.phase='failed';self.error='login_recovery_failed'

    async def screen(self,serial):
        run=self.service.android.run
        policy=await run('-s',serial,'shell','dumpsys','window','policy')
        if 'isStatusBarKeyguard=true' in policy or 'mShowingLockscreen=true' in policy:raise RecoveryError('unlock_phone')
        foreground=await run('-s',serial,'shell','dumpsys','activity','activities')
        if not any(PACKAGE in line and ('mResumedActivity' in line or 'topResumedActivity' in line) for line in foreground.splitlines()):raise RecoveryError('open_myq_required')
        path='/data/local/tmp/blueberryqdb-login.xml'
        try:
            root=ET.fromstring(await ui_dump(self.service.android,serial,path))
            if any(n.get('package') not in (None,'',PACKAGE) for n in root.iter('node')):raise RecoveryError('login_screen_obstructed')
            return root
        except ADBError:
            raise RecoveryError('android_screen_unreadable') from None

    async def tap(self,serial,node):
        match=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
        if not match:raise RecoveryError('login_ui_not_recognized')
        a,b,c,d=map(int,match.groups())
        if not (0<=a<c<=10000 and 0<=b<d<=10000):raise RecoveryError('login_ui_not_recognized')
        await self.service.android.run('-s',serial,'shell','input','tap',str((a+c)//2),str((b+d)//2))

    async def fill(self,serial,node,value):
        # ADB's shell accepts a command string. Quote secrets and prohibit input's
        # percent-s escape to preserve exact characters rather than mistyping them.
        if not value or len(value)>256 or any(ord(c)<33 or ord(c)>126 for c in value) or '%' in value:
            raise RecoveryError('login_text_unsupported')
        if node.get('text','')==value:return
        if node.get('text',''):raise RecoveryError('clear_login_field_on_phone')
        await self.tap(serial,node)
        await self.service.android.run('-s',serial,'shell','input text '+shlex.quote(value))

    async def button(self,serial,nodes,labels,required=True):
        matches=[n for n in nodes if n.get('enabled')=='true' and n.get('clickable')=='true' and
                 (n.get('text','').strip().lower() in labels or n.get('content-desc','').strip().lower() in labels)]
        if len(matches)>1:raise RecoveryError('login_ui_ambiguous')
        if not matches:
            if required:raise RecoveryError('login_ui_not_recognized')
            return False
        await self.tap(serial,matches[0]);return True
