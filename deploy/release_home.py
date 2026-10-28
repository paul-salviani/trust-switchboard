"""Run as root from the explicitly staged release folder. Narrow, backed-up deploy."""
import hashlib,json,os,pathlib,shutil,sqlite3,subprocess,time,urllib.request
ROOT=pathlib.Path('/opt/a2a');STAGE=pathlib.Path(__file__).resolve().parent
EXPECTED={'server.py':'8373964dc15355b7b2c5464b76f1c030a46feedeeb358cbef9142d5157590734',
          'mcp.py':'486d605dbd94466304f0606c47f8ccd13c67fe94af2b42cba3a399af9ae647d0',
          'geo.py':'d61bb2ed8c18816db525425755e14898d9176f4963e534c2f51fd721f1c47768'}
FILES=['server.py','mcp.py','geo.py','agent_home.py','pam.schema.json','home-requirements.txt','README.md',
       'public/home.html','public/home.js','public/home.css','public/home-api.txt','letta/bridge.mjs']
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
for name,expected in EXPECTED.items():
 if sha(ROOT/name)!=expected:raise SystemExit('Production drift: '+name)
for name in FILES:
 if not (STAGE/name).is_file():raise SystemExit('Missing staged file: '+name)
backup=ROOT/'release-backups'/time.strftime('%Y%m%dT%H%M%SZ-home',time.gmtime())
backup.mkdir(parents=True,mode=0o700)
for name in FILES:
 if (ROOT/name).exists():
  dst=backup/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,dst)
source=sqlite3.connect('/var/lib/a2a/a2a.sqlite');destination=sqlite3.connect(backup/'a2a.sqlite')
source.backup(destination);destination.close();source.close()
shutil.copytree('/var/lib/a2a/letta',backup/'letta-state')
override=pathlib.Path('/etc/systemd/system/a2a.service.d/home.conf')
if override.exists():shutil.copy2(override,backup/'home.conf')
override.parent.mkdir(parents=True,exist_ok=True)
for name in FILES:
 dst=ROOT/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(STAGE/name,dst);dst.chmod(0o644)
subprocess.run([str(ROOT/'runtime/home-venv/bin/python'),'-m','py_compile',str(ROOT/'server.py'),str(ROOT/'agent_home.py'),str(ROOT/'mcp.py'),str(ROOT/'geo.py')],check=True)
override.write_text('[Service]\nExecStart=\nExecStart=/opt/a2a/runtime/home-venv/bin/python /opt/a2a/server.py\nEnvironment=HOME_BRIDGE_TOKEN_FILE=/var/lib/a2a/letta/bridge-token\n')
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','restart','home-bridge','a2a'],check=True)
try:
 for attempt in range(20):
  try:
   with urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=3) as r:health=json.load(r)
   with urllib.request.urlopen('http://127.0.0.1:8787/api/home/public',timeout=3) as r:public=json.load(r)
   assert 'residents' in public
   break
  except Exception:
   if attempt==19:raise
   time.sleep(1)
except Exception:
 # Additive schema stays; restore previous code and interpreter without erasing any data.
 for name in FILES:
  if (backup/name).exists():shutil.copy2(backup/name,ROOT/name)
 if (backup/'home.conf').exists():shutil.copy2(backup/'home.conf',override)
 else:override.write_text('[Service]\nExecStart=\nExecStart=/usr/bin/python3 /opt/a2a/server.py\n')
 subprocess.run(['systemctl','daemon-reload']);subprocess.run(['systemctl','restart','a2a'])
 raise
receipt={'deployed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'backup':str(backup),
 'files':{name:sha(ROOT/name) for name in FILES},'health':health,
 'public_counts':{k:len(public[k]) for k in ('residents','posts','challenges','submissions','awards')}}
(STAGE/'deployment-receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt,indent=2))
