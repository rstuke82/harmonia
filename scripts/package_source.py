"""Package public source from a strict allowlist, never runtime data."""
import sys, zipfile
from pathlib import Path

def package(root, destination):
    root=Path(root).resolve();destination=Path(destination).resolve()
    files=[]
    top={'compose.yaml.example','compose.local.yaml','CONTAINER-STARTUP-CHANGES.md','SECURITY-HARDENING-CHANGES.md','SECURITY-DEPLOYMENT.md','RUNTIME-MIGRATION.md','FIX-TRACKER.md','UI-UX-FIXES.md','README.md','LICENSE','CHANGELOG.md','BUILD-NOTES.md','requirements.txt','requirements.lock','requirements.in','Dockerfile','.dockerignore','.gitignore','.env.example','docker-compose.yml','docker-compose.local.yml','REMAINING-FIXES.md','HISTORICAL-README-NOTES.md','HISTORICAL-BUILD-NOTES-20261008.9.md'}
    for p in root.rglob('*'):
        if not p.is_file():continue
        rel=p.relative_to(root)
        if not (str(rel) in top or rel.parts[0] in {'playlist_bridge','web','tests','scripts','.github'}):continue
        if any(part in {'node_modules','dist','__pycache__','.git','.venv','backups'} for part in rel.parts):continue
        if p.suffix in {'.db','.sqlite','.sqlite3','.pyc','.log','.zip','.bak'} or p.name.endswith(('-wal','-shm')):continue
        if p.name.startswith('.env') and p.name!='.env.example':continue
        if p.is_symlink():raise ValueError(f'Symlink in source manifest: {rel}')
        files.append(p)
    if not files:raise ValueError('No source files found')
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
        for p in sorted(files):archive.write(p,p.relative_to(root))
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip():raise ValueError('Archive integrity failure')
    return len(files)

if __name__=='__main__':
    print(f'Packaged {package(Path(__file__).resolve().parents[1],sys.argv[1])} source files')
