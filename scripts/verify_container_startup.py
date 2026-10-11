"""Disposable Docker integration checks; pass the local image tag as an argument."""
import subprocess, uuid, time, sys
name='harmonia-startup-test-'+uuid.uuid4().hex[:8]
config=name+'-config';music=name+'-music';image=sys.argv[1] if len(sys.argv)>1 else 'harmonia:startup-test'
def run(args,ok=True):
 r=subprocess.run(['docker',*args],capture_output=True,text=True)
 if ok and r.returncode:raise RuntimeError(r.stderr)
 return r
caps=['--read-only','--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER','--cap-add','SETUID','--cap-add','SETGID','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,noexec,nosuid,size=64m']
try:
 for v in [config,music]:run(['volume','create',v])
 run(['run','--rm','--entrypoint','python','-v',config+':/config','-v',music+':/music',image,'-c',"from pathlib import Path; import os; p=Path('/config/backups'); p.mkdir(); (p/'state.db').write_text('preserved'); os.chmod('/config',0o700); os.chmod(p,0o700); os.chmod(p/'state.db',0o600); Path('/music/sentinel').write_text('untouched'); os.chown('/music/sentinel',1234,1234)"])
 check="import os; from pathlib import Path; assert (os.getuid(),os.getgid())==(10001,10001); assert not os.getgroups(); s=Path('/proc/self/status').read_text(); assert 'CapEff:\\t0000000000000000' in s; assert 'NoNewPrivs:\\t1' in s; p=Path('/config/backups/state.db'); assert p.read_text()=='preserved'; assert p.stat().st_uid==10001; assert p.stat().st_mode&0o777==0o600; assert Path('/music/sentinel').stat().st_uid==1234; Path('/config/write-test').write_text('ok')"
 for _ in range(2):run(['run','--rm',*caps,'-v',config+':/config','-v',music+':/music:ro',image,'python','-c',check])
 print('Root-owned nested state repaired, content preserved, startup repeat passed; UID/GID 10001, no groups/capabilities, no-new-privileges confirmed; read-only music untouched.')
 run(['run','--rm','--entrypoint','python','-v',config+':/config',image,'-c',"from pathlib import Path; Path('/config/escape').symlink_to('/music/sentinel')"])
 result=run(['run','--rm',*caps,'-v',config+':/config','-v',music+':/music:ro',image,'python','-c','raise AssertionError("must not launch")'],ok=False)
 assert result.returncode==1 and 'symlinks' in result.stderr
 print('Unsafe symlink blocks startup with actionable error.')
 run(['run','--rm','--entrypoint','python','-v',config+':/config',image,'-c',"from pathlib import Path; Path('/config/escape').unlink()"])
 run(['run','--rm',*caps,'-e','HARMONIA_DATA_DIR=/data','-v',config+':/data',image,'python','-c',"import os; from pathlib import Path; assert os.getuid()==10001; assert Path('/data/backups/state.db').read_text()=='preserved'"])
 print('Explicit existing /data configuration supported.')
 run(['run','-d','--name',name,*caps,'-v',config+':/config',image])
 ready=False
 for _ in range(20):
  result=run(['exec',name,'python','-c',"import urllib.request; from pathlib import Path; s=Path('/proc/1/status').read_text(); assert 'Uid:\\t10001\\t10001\\t10001\\t10001' in s; assert 'CapEff:\\t0000000000000000' in s; urllib.request.urlopen('http://127.0.0.1:8173/api/health',timeout=2).close()"],ok=False)
  if result.returncode==0:ready=True;break
  time.sleep(.5)
 assert ready,'App health/startup failed (logs may contain private setup key)'
 print('Actual Harmonia service is healthy and PID 1 runs as UID 10001 with no capabilities.')
finally:
 run(['rm','-f',name],ok=False)
 for v in [config,music]:run(['volume','rm',v],ok=False)
