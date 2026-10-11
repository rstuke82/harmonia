"""Exercise native dependencies in the built runtime, including artwork codecs."""
from io import BytesIO
from pathlib import Path
import gzip
import sqlite3
import ssl
import subprocess
from zoneinfo import ZoneInfo
import zlib

from PIL import Image
from Levenshtein import distance
from pydantic import BaseModel


def main():
    packages = [dict(line.split(':', 1) for line in record.splitlines() if ':' in line)
                for record in Path('/lib/apk/db/installed').read_text().split('\n\n')]
    version = next(package['V'] for package in packages if package.get('P') == 'zlib')
    package = 'zlib-' + version
    assert subprocess.check_output(['apk', 'version', '-t', version, '1.3.2-r1'], text=True).strip() in ('=', '>'), package
    payload = b'Harmonia runtime compression check' * 100
    assert zlib.decompress(zlib.compress(payload)) == payload
    assert gzip.decompress(gzip.compress(payload)) == payload
    for codec in ('PNG', 'JPEG', 'WEBP'):
        encoded = BytesIO()
        Image.new('RGB', (32, 32), (30, 100, 180)).save(encoded, format=codec)
        with Image.open(BytesIO(encoded.getvalue())) as artwork:
            artwork.verify()
        with Image.open(BytesIO(encoded.getvalue())) as artwork:
            artwork.load()
            assert artwork.size == (32, 32)
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE state (value TEXT)')
        db.execute('INSERT INTO state VALUES (?)', ('saved inventory',))
        assert db.execute('SELECT value FROM state').fetchone()[0] == 'saved inventory'
    assert distance('Harmonia', 'Harmonia') == 0
    class Value(BaseModel):
        count: int
    assert Value(count='25').count == 25
    assert ZoneInfo('America/Chicago').key == 'America/Chicago'
    assert ssl.create_default_context().get_ca_certs()
    print(f'{package}; runtime zlib {zlib.ZLIB_RUNTIME_VERSION}; SQLite {sqlite3.sqlite_version}; native codecs, matching, validation, timezone and TLS trust passed.')


if __name__ == '__main__':
    main()
