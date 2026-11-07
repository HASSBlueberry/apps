"""An authenticated ingress controller. Android runs on a separate SSH host."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import secrets
import socket

import aiohttp
from aiohttp import web
import asyncssh

PUBLIC={'ssh_host','ssh_port','ssh_username','ssh_fingerprint','runtime_mode','qdb_url','adb_target','authorize_key','setup_modules','android_image','myq_apk_url','install_myq'}
SECRET={'ssh_password','qdb_key'}
RESTORE_TASK=web.AppKey('restore_task',asyncio.Task)


class DroidError(ValueError):pass


def private_write(path,data):
    temporary=path.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as stream:
        os.fchmod(stream.fileno(),0o600);stream.write(data)
    temporary.replace(path)


def save(path,value):
    private_write(path,json.dumps(value).encode())


def target(values):
    host=values.get('ssh_host','')
    user=values.get('ssh_username','')
    port=int(values.get('ssh_port',22))
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}',host):raise DroidError('invalid_ssh_host')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}',user):raise DroidError('invalid_ssh_username')
    if not 1<=port<=65535:raise DroidError('invalid_ssh_port')
    return host,user,port


class Controller:
    def __init__(self,directory,runtime='/runtime',listen_host='0.0.0.0',listen_port=15555):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.directory/'state.json'
        self.state=json.loads(self.path.read_text()) if self.path.exists() else {'api_key':secrets.token_urlsafe(32)}
        self.state.setdefault('runtime_mode','auto');self.state.setdefault('ssh_port',22)
        self.state.setdefault('ssh_username','root');self.state.setdefault('authorize_key',True)
        self.state.setdefault('setup_modules',True)
        self.state.setdefault('install_myq',True)
        self.state.setdefault('adb_target',socket.gethostname()+':15555')
        self.csrf=secrets.token_urlsafe(32);self.runtime=Path(runtime);self.listen_host=listen_host;self.listen_port=listen_port
        self.key_path=self.directory/'identity'
        if not self.key_path.exists():
            key=asyncssh.generate_private_key('ssh-ed25519')
            private_write(self.key_path,key.export_private_key())
        self.connection=None;self.listener=None;self.qdb_registered=False;self.task=None;self.phase='idle';self.error=None;self.failure_class=None;self.auth_blocked=False
        self.session=None
        save(self.path,self.state)

    def status(self):
        return {**{k:self.state.get(k) for k in PUBLIC},'phase':self.phase,'error':self.error,
                'failure_class':self.failure_class,
                'connected':bool(self.connection and not self.connection.is_closed()),
                'running':bool(self.task and not self.task.done()),
                'password_saved':bool(self.state.get('ssh_password')),
                'qdb_registered':self.qdb_registered,
                'qdb_key_saved':bool(self.state.get('qdb_key')),
                'public_key':asyncssh.read_private_key(self.key_path).export_public_key().decode().strip()}

    async def configure(self,values):
        if self.task and not self.task.done():raise DroidError('deployment_in_progress')
        if not isinstance(values,dict):raise DroidError('invalid_configuration')
        for name in (PUBLIC|SECRET)-{'ssh_port','authorize_key','setup_modules','install_myq'}:
            if name in values and (not isinstance(values[name],str) or len(values[name])>32768):raise DroidError('invalid_configuration')
        updated=dict(self.state)
        for name in PUBLIC|SECRET:
            if name in values and (name not in SECRET or values[name]):updated[name]=values[name]
        target(updated)
        image=updated.get('android_image','')
        if image and not re.fullmatch(r'[A-Za-z0-9/._:-]+@sha256:[a-f0-9]{64}',image):raise DroidError('image_digest_required')
        if updated.get('runtime_mode') not in ('auto','direct','vm'):raise DroidError('invalid_runtime_mode')
        url=updated.get('qdb_url','')
        from urllib.parse import urlsplit
        parsed=urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
            raise DroidError('invalid_qdb_url')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,253}:15555',updated.get('adb_target','')):raise DroidError('invalid_adb_target')
        if self.state.get('remote_job') and any(updated.get(name)!=self.state.get(name) for name in ('ssh_host','ssh_port','ssh_username','runtime_mode','android_image')):
            raise DroidError('remote_install_still_running')
        if values.get('private_key'):
            try:key=asyncssh.import_private_key(values['private_key'])
            except (asyncssh.KeyImportError,ValueError):raise DroidError('invalid_private_key') from None
            private_write(self.key_path,key.export_private_key())
        await self.disconnect()
        updated['deployed']=False
        self.state=updated;self.auth_blocked=False;save(self.path,self.state)

    async def fingerprint(self,values):
        host,_,port=target({**self.state,**values})
        try:
            async with asyncio.timeout(15):key=await asyncssh.get_server_host_key(host,port=port)
            if key is None:raise DroidError('ssh_key_unavailable')
            return {'fingerprint':key.get_fingerprint('sha256')}
        except (OSError,asyncssh.Error,TimeoutError):raise DroidError('ssh_probe_failed') from None

    async def connect(self):
        if self.auth_blocked:raise DroidError('ssh_auth_failed')
        await self.disconnect()
        host,user,port=target(self.state)
        expected=self.state.get('ssh_fingerprint','')
        if not expected.startswith('SHA256:'):raise DroidError('ssh_fingerprint_required')
        try:
            async with asyncio.timeout(20):
                key=await asyncssh.get_server_host_key(host,port=port)
                if key is None or not secrets.compare_digest(key.get_fingerprint('sha256'),expected):raise DroidError('ssh_host_key_changed')
                self.connection=await asyncssh.connect(host,port=port,username=user,
                    known_hosts=([key],[],[]),client_keys=[str(self.key_path)],
                    password=self.state.get('ssh_password') or None,agent_path=None,
                    keepalive_interval=30,keepalive_count_max=3)
            self.listener=await self.connection.forward_local_port(self.listen_host,self.listen_port,'127.0.0.1',6555)
        except DroidError:raise
        except asyncssh.PermissionDenied:
            await self.disconnect();self.auth_blocked=True
            raise DroidError('ssh_auth_failed') from None
        except (OSError,asyncssh.Error,TimeoutError):
            await self.disconnect()
            raise DroidError('ssh_connect_failed') from None

    async def disconnect(self):
        self.qdb_registered=False
        listener,self.listener=self.listener,None
        connection,self.connection=self.connection,None
        if listener:
            listener.close()
            with suppress(TimeoutError):
                async with asyncio.timeout(5):await listener.wait_closed()
        if connection:
            connection.close()
            try:
                async with asyncio.timeout(10):await connection.wait_closed()
            except TimeoutError:connection.abort()

    async def qdb(self,method,path,payload=None,binary=False):
        if not self.state.get('qdb_key'):raise DroidError('qdb_key_required')
        try:
            async with self.session.request(method,self.state['qdb_url'].rstrip('/')+path,
                headers={'Authorization':'Bearer '+self.state['qdb_key']},json=payload,
                timeout=aiohttp.ClientTimeout(total=120),allow_redirects=False) as response:
                if response.status!=200:
                    # Only stable QDB codes are returned to ingress.
                    value=await response.json()
                    reason=value.get('error','qdb_request_failed')
                    raise DroidError(reason if re.fullmatch(r'[a-z_]{1,80}',reason) else 'qdb_request_failed')
                if path=='/v1/android/target':self.qdb_registered=True
                return await response.read() if binary else await response.json()
        except DroidError:raise
        except (aiohttp.ClientError,TimeoutError,KeyError,ValueError):raise DroidError('qdb_unavailable') from None

    async def sync_adb_key(self):
        material=await self.qdb('GET','/v1/android/adb-public-key')
        key=material.get('public_key','').split()[0]
        if not re.fullmatch(r'[A-Za-z0-9+/=]{100,4096}',key):raise DroidError('invalid_adb_public_key')
        async with self.connection.start_sftp_client() as sftp:
            await sftp.makedirs('.local/share/blueberry-droid',exist_ok=True)
            async with sftp.open('.local/share/blueberry-droid/adb-key.pub','w') as output:await output.write(key+' blueberryqdb\n')

    async def deploy(self):
        self.state['tracking_paused']=False;save(self.path,self.state)
        if self.task and not self.task.done():raise DroidError('deployment_in_progress')
        self.task=asyncio.create_task(self._deploy())

    async def _deploy(self):
        self.error=None;self.phase='connecting'
        try:
            await self.connect()
            if not self.state.get('remote_job'):
                self.phase='uploading'
                await self.sync_adb_key()
                async with self.connection.start_sftp_client() as sftp:
                    await sftp.makedirs('.local/share/blueberry-droid/runtime',exist_ok=True)
                    if self.state.get('authorize_key'):
                        await sftp.makedirs('.ssh',exist_ok=True);await sftp.chmod('.ssh',0o700)
                        public=asyncssh.read_private_key(self.key_path).export_public_key().decode().strip()
                        try:
                            async with sftp.open('.ssh/authorized_keys','r') as f:existing=await f.read(1024*1024)
                        except asyncssh.SFTPNoSuchFile:existing=''
                        if public not in existing.splitlines():
                            async with sftp.open('.ssh/authorized_keys','a') as f:await f.write('\n'+public+'\n')
                        await sftp.chmod('.ssh/authorized_keys',0o600)
                    # Upload only reviewed source files, excluding local data and SSH keys.
                    for file in self.runtime.rglob('*'):
                        if not file.is_file() or '__pycache__' in file.parts:continue
                        relative=file.relative_to(self.runtime)
                        remote='.local/share/blueberry-droid/runtime/'+relative.as_posix()
                        await sftp.makedirs(str(Path(remote).parent),exist_ok=True)
                        if relative.as_posix()=='Dockerfile' and self.state.get('android_image'):
                            source=re.sub(r'^ARG ANDROID_IMAGE=.*$', 'ARG ANDROID_IMAGE='+self.state['android_image'],file.read_text(),count=1,flags=re.M)
                            source=re.sub(r'^ARG INSTALL_GAPPS=.*$', 'ARG INSTALL_GAPPS=0',source,count=1,flags=re.M)
                            async with sftp.open(remote,'w') as output:await output.write(source)
                        else:await sftp.put(file,remote)
                    await sftp.put(Path(__file__).parent/'deploy.sh','.local/share/blueberry-droid/deploy.sh')
                    await sftp.put(Path(__file__).parent/'authorize-adb.sh','.local/share/blueberry-droid/authorize-adb.sh')
                job=secrets.token_hex(12)
                base='.local/share/blueberry-droid/jobs/'+job
                mode=self.state['runtime_mode']
                script=("#!/bin/sh\numask 077\nsh .local/share/blueberry-droid/deploy.sh "+mode+
                        " >"+base+"/deploy.log 2>&1\nresult=$?\nprintf '%s\\n' \"$result\" >"+base+
                        "/exit.tmp\nchmod 0644 "+base+"/exit.tmp\nmv "+base+"/exit.tmp "+base+"/exit\nexit \"$result\"\n")
                async with self.connection.start_sftp_client() as sftp:
                    await sftp.makedirs(base,exist_ok=True);await sftp.chmod(base,0o700)
                    async with sftp.open(base+'/run.sh','w') as output:await output.write(script)
                command=('prefix=; if [ "$(id -u)" != 0 ]; then prefix="sudo -n"; fi; '
                         '$prefix systemd-run --quiet --no-block --collect --unit=blueberry-droid-'+job+
                         ' --property=Type=oneshot --property=Delegate=yes --property=TimeoutStartSec=1800 --working-directory="$PWD" /bin/sh "$PWD/'+base+'/run.sh"')
                result=await self.connection.run(command,check=False,timeout=20)
                if result.exit_status:raise DroidError('remote_job_start_failed')
                self.state['remote_job']=job;save(self.path,self.state)
            self.phase='installing_runtime'
            job=self.state['remote_job']
            if not re.fullmatch(r'[a-f0-9]{24}',job):raise DroidError('invalid_remote_job')
            base='.local/share/blueberry-droid/jobs/'+job
            deadline=asyncio.get_running_loop().time()+1800
            while asyncio.get_running_loop().time()<deadline:
                try:
                    if not self.connection or self.connection.is_closed():await self.connect()
                    result=await self.connection.run('if [ -f '+base+'/exit ]; then cat '+base+'/exit; fi',check=False,timeout=20)
                    code=result.stdout.strip()
                    if code:
                        if not re.fullmatch(r'[0-9]{1,3}',code):raise DroidError('invalid_remote_job_result')
                        self.state.pop('remote_job',None);save(self.path,self.state)
                        if code!='0':
                            command='prefix=; if [ \"$(id -u)\" != 0 ]; then prefix=\"sudo -n\"; fi; $prefix head -c 2097152 '+base+'/deploy.log'
                            log=await self.connection.run(command,check=False,timeout=20)
                            private_write(self.directory/'deploy.log',log.stdout.encode())
                            raise DroidError('remote_deployment_failed_'+code)
                        break
                except (OSError,asyncssh.Error,TimeoutError):
                    await self.disconnect()
                await asyncio.sleep(5)
            else:raise DroidError('remote_install_timeout')
            self.state['deployed']=True;save(self.path,self.state)
            self.phase='connecting_qdb'
            await self.qdb('POST','/v1/android/target',{'target':self.state['adb_target']})
            self.phase='waiting_for_android'
            deadline=asyncio.get_running_loop().time()+1200
            # Integrity modules first: they reboot the device, and MyQ must be
            # installed onto that fully configured system, not before it.
            modules_done=not self.state.get('setup_modules')
            while asyncio.get_running_loop().time()<deadline:
                try:
                    status=await self.qdb('GET','/v1/android/status')
                except DroidError:
                    await asyncio.sleep(10)
                    continue
                if status.get('boot_completed'):
                    if not modules_done:
                        self.phase='configuring_modules'
                        await self.qdb('POST','/v1/android/modules',{})
                        while asyncio.get_running_loop().time()<deadline:
                            await asyncio.sleep(3)
                            module_status=(await self.qdb('GET','/v1/android/status')).get('module_setup',{})
                            if module_status.get('error'):raise DroidError(module_status['error'])
                            if module_status.get('phase')=='reboot_requested':break
                        else:raise DroidError('module_setup_timeout')
                        # Setup requested a reboot; wait for it to start, then
                        # loop back through boot so MyQ installs afterwards.
                        self.phase='modules_installed_reboot_requested'
                        reboot_deadline=asyncio.get_running_loop().time()+300
                        while asyncio.get_running_loop().time()<reboot_deadline:
                            await asyncio.sleep(5)
                            try:rebooting=not (await self.qdb('GET','/v1/android/status')).get('boot_completed')
                            except DroidError:rebooting=True
                            if rebooting:break
                        else:raise DroidError('android_reboot_timeout')
                        modules_done=True
                        continue
                    if self.state.get('install_myq') and not status.get('myq_installed'):
                        self.phase='installing_myq'
                        source={'url':self.state['myq_apk_url']} if self.state.get('myq_apk_url') else {'source':'usb'}
                        await self.qdb('POST','/v1/android/myq',source)
                        while asyncio.get_running_loop().time()<deadline:
                            await asyncio.sleep(3)
                            app_status=(await self.qdb('GET','/v1/android/status')).get('myq_install',{})
                            if app_status.get('error'):raise DroidError(app_status['error'])
                            if app_status.get('phase')=='installed':break
                        else:raise DroidError('myq_install_timeout')
                    self.phase='ready';return
                await asyncio.sleep(10)
            raise DroidError('android_boot_timeout')
        except asyncio.CancelledError:self.phase='cancelled';raise
        except DroidError as error:self.phase='failed';self.error=str(error)
        except (OSError,asyncssh.Error,TimeoutError) as error:
            self.phase='failed';self.error='deployment_connection_failed';self.failure_class=type(error).__name__


def create_app(controller):
    @web.middleware
    async def protect(request,handler):
        key=request.headers.get('Authorization','').removeprefix('Bearer ')
        authenticated=secrets.compare_digest(key,controller.state['api_key'])
        ingress=request.remote=='172.30.32.2'
        if not ingress and not authenticated:raise web.HTTPUnauthorized()
        if ingress and request.method!='GET' and not authenticated:
            if not secrets.compare_digest(request.headers.get('X-Droid-CSRF',''),controller.csrf):raise web.HTTPForbidden()
        try:
            response=await handler(request)
            response.headers['Cache-Control']='no-store'
            response.headers['X-Content-Type-Options']='nosniff'
            response.headers['Referrer-Policy']='no-referrer'
            return response
        except DroidError as error:return web.json_response({'error':str(error)},status=409)
        except (ValueError,TypeError,KeyError):return web.json_response({'error':'invalid_request'},status=400)
    app=web.Application(middlewares=[protect],client_max_size=301*1024*1024)
    async def home(request):return web.Response(text=(Path(__file__).parent/'index.html').read_text().replace('__CSRF__',controller.csrf),content_type='text/html')
    async def status(request):return web.json_response(controller.status())
    async def configure(request):await controller.configure(await request.json());return web.json_response(controller.status())
    async def fingerprint(request):return web.json_response(await controller.fingerprint(await request.json()))
    async def deploy(request):await controller.deploy();return web.json_response(controller.status())
    async def reconnect(request):
        await controller.connect()
        await controller.qdb('POST','/v1/android/target',{'target':controller.state['adb_target']})
        controller.state['deployed']=True;save(controller.path,controller.state)
        return web.json_response(controller.status())
    async def cancel(request):
        if request is not None:
            controller.state['tracking_paused']=True;save(controller.path,controller.state)
        if controller.task:
            controller.task.cancel()
            with suppress(asyncio.CancelledError):await controller.task
        return web.json_response(controller.status())
    async def runtime(request):return web.json_response(await controller.qdb('GET','/v1/android/status'))
    async def screen(request):return web.Response(body=await controller.qdb('GET','/v1/android/screenshot',binary=True),content_type='image/png')
    async def input_event(request):return web.json_response(await controller.qdb('POST','/v1/android/input',await request.json()))
    async def open_myq(request):return web.json_response(await controller.qdb('POST','/v1/android/myq/open',{}))
    async def install_myq(request):return web.json_response(await controller.qdb('POST','/v1/android/myq',await request.json()))
    async def upload_myq(request):
        reader=await request.multipart();field=await reader.next()
        if field is None or field.name!='file':raise DroidError('apk_file_required')
        import tempfile
        fd,name=tempfile.mkstemp(prefix='myq-upload-',dir=controller.directory)
        source=None
        try:
            with os.fdopen(fd,'wb') as output:
                size=0
                while chunk:=await field.read_chunk(65536):
                    size+=len(chunk)
                    if size>300*1024*1024:raise DroidError('apk_download_too_large')
                    output.write(chunk)
            source=open(name,'rb')
            async with controller.session.post(controller.state['qdb_url'].rstrip('/')+'/v1/android/myq/upload',
                headers={'Authorization':'Bearer '+controller.state['qdb_key']},data={'file':source},timeout=aiohttp.ClientTimeout(total=300),allow_redirects=False) as response:
                value=await response.json()
                if response.status!=200:raise DroidError(value.get('error','qdb_upload_failed'))
                return web.json_response(value)
        finally:
            if source:source.close()
            with suppress(OSError):Path(name).unlink()
    async def modules(request):return web.json_response(await controller.qdb('POST','/v1/android/modules',{}))
    async def restore():
        delay=10
        while True:
            try:
                if controller.auth_blocked:
                    await asyncio.sleep(300)
                    continue
                if controller.state.get('remote_job') and not controller.state.get('tracking_paused') and not (controller.task and not controller.task.done()):
                    await controller.deploy()
                elif controller.state.get('deployed') and not (controller.task and not controller.task.done()) and (not controller.connection or controller.connection.is_closed() or not controller.qdb_registered):
                    if not controller.connection or controller.connection.is_closed():await controller.connect()
                    await controller.qdb('POST','/v1/android/target',{'target':controller.state['adb_target']})
                    controller.phase='connected';controller.error=None;delay=10
            except DroidError as error:
                controller.error=str(error);delay=min(300,delay*2)
            await asyncio.sleep(delay)
    async def startup(app):
        controller.session=aiohttp.ClientSession()
        app[RESTORE_TASK]=asyncio.create_task(restore())
    async def cleanup(app):
        app[RESTORE_TASK].cancel()
        with suppress(asyncio.CancelledError):await app[RESTORE_TASK]
        await cancel(None);await controller.disconnect()
        if controller.session:await controller.session.close()
    app.on_startup.append(startup);app.on_cleanup.append(cleanup)
    app.router.add_get('/',home);app.router.add_get('/status',status)
    app.router.add_post('/configure',configure);app.router.add_post('/fingerprint',fingerprint)
    app.router.add_post('/deploy',deploy);app.router.add_post('/reconnect',reconnect);app.router.add_post('/cancel',cancel)
    app.router.add_get('/android/status',runtime);app.router.add_get('/android/screen',screen)
    app.router.add_post('/android/input',input_event);app.router.add_post('/android/modules',modules)
    app.router.add_post('/android/myq',install_myq);app.router.add_post('/android/myq/open',open_myq);app.router.add_post('/android/myq/upload',upload_myq)
    return app


if __name__=='__main__':web.run_app(create_app(Controller('/data')),host='0.0.0.0',port=8092)
