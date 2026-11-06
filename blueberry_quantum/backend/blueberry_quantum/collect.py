"""Read-only collection of public SPA endpoint and profile evidence."""
import argparse
import asyncio
import json
from pathlib import Path
from aiohttp import ClientSession,TCPConnector
from .providers import G3100Provider
async def collect(args):
    async with ClientSession(connector=TCPConnector(ssl=False if args.self_signed else True)) as session:
        report=await G3100Provider(session,{'url':args.url}).inspect()
        Path(args.output).write_text(json.dumps(report,indent=2)+'\n')
        print(f"Inspected {len(report['assets'])} assets. Report saved to {args.output}")
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='https://192.168.1.1')
    parser.add_argument('--self-signed',action='store_true')
    parser.add_argument('--output',default='quantum-spa-report.json')
    asyncio.run(collect(parser.parse_args()))
