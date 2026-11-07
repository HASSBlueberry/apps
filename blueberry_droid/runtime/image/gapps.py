"""Build a checksummed Google services overlay for the matching Android 13 ABI."""
import hashlib
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile

ASSETS={
 'amd64':('x86_64','a5b82ef50974d83df1a635556039fc625a0ab88d4fb3911d806de6168ce21bb6'),
 'aarch64':('arm64','44bd297bea0b3b7f6b36f295b429c8bdce432eba55ec1ddd74f6920e22f65ff8'),
}


def prepare(architecture,destination,archive=None):
    abi,expected=ASSETS[architecture]
    with tempfile.TemporaryDirectory(prefix='gapps-') as temporary:
        package=Path(archive) if archive else Path(temporary)/'gapps.zip'
        if archive is None:
            url=f'https://github.com/s1204IT/MindTheGappsBuilder/releases/download/20240619/MindTheGapps-13.0.0-{abi}-20240619.zip'
            with urllib.request.urlopen(url,timeout=60) as response,package.open('wb') as output:
                size=0
                while chunk:=response.read(1024*1024):
                    size+=len(chunk)
                    if size>512*1024*1024:raise ValueError('Google services download is too large')
                    output.write(chunk)
        digest=hashlib.sha256()
        with package.open('rb') as source:
            while chunk:=source.read(1024*1024):digest.update(chunk)
        if digest.hexdigest()!=expected:raise ValueError('Google services checksum mismatch')
        root=Path(destination)
        with zipfile.ZipFile(package) as bundle:
            if sum(info.file_size for info in bundle.infolist())>2*1024*1024*1024:raise ValueError('Google services overlay is too large')
            for info in bundle.infolist():
                relative=PurePosixPath(info.filename)
                if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe Google services path')
                if not relative.parts or relative.parts[0]!='system':continue
                if stat.S_ISLNK(info.external_attr>>16):raise ValueError('Unexpected Google services symlink')
                target=root.joinpath(*relative.parts)
                if info.is_dir():target.mkdir(parents=True,exist_ok=True);target.chmod(0o755);continue
                target.parent.mkdir(parents=True,exist_ok=True)
                with bundle.open(info) as source,target.open('wb') as output:shutil.copyfileobj(source,output,1024*1024)
                target.chmod(0o755 if (info.external_attr>>16)&0o111 else 0o644)


if __name__=='__main__':prepare(sys.argv[1],sys.argv[2])
