
from pathlib import Path
import json,sqlite3,hashlib,subprocess,os,sys
root=Path(__file__).resolve().parent.parent
phase,report_name,worktree,revision,previous=sys.argv[1:]
w=Path(worktree);report=root/'runtime-data/runtime'/report_name/'environment-run.json'
j=json.loads(report.read_text())
assert j['status']=='ready' and j['runtime']['dataRoot']==str(root/'runtime-data') and j['runtime']['environmentId']=='dev'
be=j['services']['backend']['containerIdentity']['containerId']
info=json.loads(subprocess.check_output(['docker','inspect',be],text=True))[0]
assert info['Name']=='/digital-souls-dev-backend'
assert info['Config']['Image']=='digital-souls-voice-quality/backend:'+revision
native=json.loads(subprocess.check_output(['docker','exec',be,'python','-m','app.livekit_transport.native_build'],text=True))
assert native['status']=='verified'
(root/phase/'native-sdk.json').write_text(json.dumps(native,indent=2)+'\n')
c=sqlite3.connect(root/'runtime-data/conversation-history.db');c.row_factory=sqlite3.Row
rows=[dict(x) for x in c.execute('select * from conversation_turns order by turn_id')]
signs=[{'id':x['turn_id'],'status':x['status'],'hash':hashlib.sha256(json.dumps(x,sort_keys=True).encode()).hexdigest(),'assistantLength':len(x['assistant_content'] or '')} for x in rows]
prior=json.loads((root/previous/'database-snapshot.json').read_text())['rows']
assert all(x in signs for x in prior)
privacy=[x for x in rows if x['status']=='privacy_skipped']
assert len(privacy)>=1 and all(x['user_content'] is None and x['assistant_content'] is None for x in privacy)
interrupted=[x for x in rows if x['status']=='interrupted']
assert any(len(x['assistant_content'] or '')==9 for x in interrupted)
(root/phase/'database-snapshot.json').write_text(json.dumps({'rows':signs,'priorRowsUnchanged':True,'nativeStatus':native['status'],'interruptedPrefixLength':9},indent=2)+'\n')
print(json.dumps({'phase':phase,'rows':len(rows),'priorRowsUnchanged':True,'native':native['status'],'statuses':{s:sum(x['status']==s for x in rows) for s in set(x['status'] for x in rows)}}),flush=True)
env=os.environ|{'DS_ENVIRONMENT_ID':'dev','DS_DATA_DIR':str(root/'runtime-data')}
down=subprocess.run([str(w/'backend/.venv/bin/python'),str(w/'environments/environment_cli.py'),'down','--run-report',str(report)],cwd=w,env=env,capture_output=True,text=True,timeout=60)
(root/phase/'down.log').write_text(down.stdout+down.stderr)
assert down.returncode==0
print('DOWN 0',flush=True)
