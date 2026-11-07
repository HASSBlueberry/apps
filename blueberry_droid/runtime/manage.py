#!/usr/bin/env python3
"""Manage only the dedicated Blueberry Android container on a Linux host."""
import argparse
import json
from pathlib import Path
import subprocess

NAME='blueberry-android'
IMAGE='localhost/blueberry-android:0.1.0'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['build','start','status','stop'])
    parser.add_argument('--engine',choices=['podman','docker'],default='podman')
    parser.add_argument('--rootful',action='store_true')
    parser.add_argument('--vm',action='store_true',help='Use an isolated x86-64 KVM guest')
    parser.add_argument('--bind',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=6555)
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('Invalid port')
    import ipaddress
    address=ipaddress.ip_address(args.bind)
    if address.version!=4:parser.error('Use an IPv4 binding')
    name=NAME+'-vm' if args.vm else NAME
    image='localhost/blueberry-android-vm:0.1.0' if args.vm else IMAGE
    engine=(['sudo','-n'] if args.rootful else [])+[args.engine]
    def run(arguments,**kwargs):return subprocess.run(engine+arguments,check=True,**kwargs)
    if args.action=='build':
        import platform
        arch={'x86_64':'amd64','aarch64':'aarch64'}[platform.machine()]
        if args.vm and arch!='amd64':parser.error('The VM wrapper currently requires amd64')
        context=Path(__file__).resolve().parent
        options=['-f',str(context/'vm/Dockerfile')] if args.vm else []
        run(['build',*options,'--build-arg','BUILD_ARCH='+arch,'-t',image,str(context)])
    elif args.action=='start':
        # Refuse name collisions, preserving data and any existing instance.
        found=subprocess.run(engine+['container','exists',name],capture_output=True) if args.engine=='podman' else subprocess.run(engine+['inspect',name],capture_output=True)
        if found.returncode==0:parser.error('Container already exists. Inspect it rather than replacing it.')
        if found.returncode not in (1,):parser.error('Container engine is unavailable')
        runtime=['--device','/dev/kvm','--security-opt','label=disable','--network','host','--env','BLUEBERRY_BIND_HOST='+args.bind,'--env','BLUEBERRY_ADB_PORT='+str(args.port),'--memory','8g'] if args.vm else ['--privileged','--memory','4g']
        volume='blueberry-android-vm-data' if args.vm else 'blueberry-android-data'
        mapping=[] if args.vm else ['-p',f'{args.bind}:{args.port}:5555']
        run(['run','--detach','--restart','unless-stopped','--name',name,*runtime,'--pids-limit','4096',
             *mapping,'-v',volume+':/data',image])
    elif args.action=='stop':run(['stop',name])
    else:
        result=run(['inspect',name],capture_output=True,text=True)
        state=json.loads(result.stdout)[0]
        print(json.dumps({'name':name,'running':state['State']['Running'],'status':state['State']['Status']}))


if __name__=='__main__':main()
