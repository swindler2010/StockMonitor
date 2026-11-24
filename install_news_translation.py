"""Explicit one-time setup, never called by the Stock Monitor runtime.

Run in the SM Python environment after installing requirements-news-translation.txt.
Keeps four ZIP model packs in the existing dependency directory, no extraction.
"""
import hashlib
import json
from pathlib import Path
import sys
import time
import requests

PACKS = [
    ('en_ru', '1_9', 195746693, '591d743ae103752b88ffc38785c50421320f4eff93c8967e0d3d2e14d4e27811'),
    ('en_de', '1_3', 150508297, '6cd847f0c06c9c66013e6b0932e07fd54a6d90894659c02bf6c5247b72fb25b1'),
    ('de_en', '1_3', 150512831, 'becc2b0011f8249fcb89be9ecb75ba0d876b1fab93c28ee6ff0420936897d637'),
    ('ru_en', '1_9', 156239112, 'e9ba8bf722d10a4a4c39f74289d5938fd47eac08dbe4ed0afd22d89445a5c3ac'),
]


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
    return digest.hexdigest()


def install(directory=None):
    directory = Path(directory or Path(sys.prefix)/'Дополнительные пакеты')
    if not directory.is_dir(): raise ValueError('Select an existing dependency directory')
    manifest = []
    for pair, version, size, digest in PACKS:
        url = f'https://argos-net.com/v1/translate-{pair}-{version}.argosmodel'
        path = directory/('sm-translate-'+pair+'.argosmodel')
        if not (path.exists() and path.stat().st_size == size and checksum(path) == digest):
            temporary = path.with_suffix('.pending')
            started = time.monotonic(); count = 0
            try:
                with requests.get(url, timeout=(5, 20), stream=True) as response:
                    response.raise_for_status()
                    if response.status_code != 200: raise ValueError('MODEL_HTTP_NOT_200')
                    with temporary.open('wb') as stream:
                        for chunk in response.iter_content(256*1024):
                            count += len(chunk)
                            if count > size or time.monotonic()-started > 300: raise ValueError('MODEL_DOWNLOAD_LIMIT')
                            stream.write(chunk)
                if count != size or checksum(temporary) != digest: raise ValueError('MODEL_CHECKSUM_MISMATCH')
                temporary.replace(path)
            finally:
                if temporary.exists(): temporary.unlink()
        manifest.append(dict(pair=pair, url=url, sha256=digest, size=size))
        print(pair, 'VERIFIED', flush=True)
    path = directory/'sm-translation-models.json'; temporary = path.with_suffix('.pending')
    temporary.write_text(json.dumps(manifest, indent=2), encoding='utf-8'); temporary.replace(path)


if __name__ == '__main__': install(sys.argv[1] if len(sys.argv) > 1 else None)
