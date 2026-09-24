import os,sys,pathlib,subprocess,socket,time,json,uuid,signal,urllib.request,shutil,tempfile
root=pathlib.Path(sys.argv[1]).resolve(strict=True)
py=root/'backend/.venv/bin/python'
if not py.is_file():raise SystemExit('指定worktreeのbackend/.venvにPythonがありません')
node=shutil.which('node')
if node is None:raise SystemExit('PATH上にNode.jsがありません')
run=pathlib.Path(tempfile.mkdtemp(prefix='ds486-it2-'))
print('EVIDENCE',str(run),flush=True)
for port,typ in [(15195,socket.SOCK_STREAM),(19495,socket.SOCK_STREAM),(19586,socket.SOCK_STREAM),(19587,socket.SOCK_STREAM),(19588,socket.SOCK_DGRAM)]:
 with socket.socket(socket.AF_INET,typ) as s:
  s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',port))
key='epic480test';secret=uuid.uuid4().hex+uuid.uuid4().hex
config=run/'livekit.yaml';config.write_text('port: 19586\nbind_addresses: [127.0.0.1]\nrtc:\n  tcp_port: 19587\n  udp_port: 19588\n  use_external_ip: false\nkeys:\n  '+key+': '+secret+'\n');config.chmod(0o600)
env={k:v for k,v in os.environ.items() if not k.startswith(('DS_','LIVEKIT_','INFERENCE_','OLLAMA_','TTS_','WHISPER_','IRODORI_','VOICEVOX_'))}
env.update(SCREEN_ALLOWED_ORIGIN='http://127.0.0.1:15195',PYTHON_DOTENV_DISABLED='1',PYTHONFAULTHANDLER='1',PYTHONPATH=str(root/'backend'),DS_ENVIRONMENT_ID='test',DS_DATA_DIR=str(run/'data-backend'),RAG_ENABLED='false',LIVEKIT_URL='ws://127.0.0.1:19586',LIVEKIT_API_KEY=key,LIVEKIT_API_SECRET=secret,LIVEKIT_TEST_BACKEND_URL='http://127.0.0.1:19495')
for role in ['CHAT','PRIVACY','MEMORY_EXTRACTION','MEMORY_CONSOLIDATION']:
 env['INFERENCE_TARGET_'+role]='ollama/gemma4:e4b';env['INFERENCE_TARGET_'+role+'_MAX_INPUT_TOKENS']='7168' if role=='CHAT' else '7680';env['INFERENCE_TARGET_'+role+'_MAX_OUTPUT_TOKENS']='1024' if role=='CHAT' else '512'
env['INFERENCE_TARGET_EMBEDDING']='ollama/nomic-embed-text:latest';env['INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS']='8192'
sys.path.insert(0,str(root/'scripts/voice_quality'))
from run_pilot import measurement_revision
revision=measurement_revision(root)
env.update(DS_PROFILE='integration-voice',VOICE_MEASUREMENT_KIND='controlled_baseline',VOICE_CONTROLLED_TRACE_PATH=str(run/'data-backend/voice-metrics/controlled-trace.jsonl'),VOICE_QUALITY_MEASUREMENT_REVISION=revision,VOICE_QUALITY_MANIFEST_PATH=str(run/'trial-manifest.json'),VOICE_QUALITY_PILOT_TRIALS='1',VOICE_QUALITY_ISOLATED_NORMAL_PHASE='measured',VOICE_QUALITY_SCHEDULED_FIXTURE='1')
container=None;backend=None;frontend=None;result={'revision':revision,'measurement':'existing quality spec with isolated process supervisor; not formal100trial/nativeSDKpatched acceptance'}
try:
 name='ds486-it2-'+uuid.uuid4().hex[:8]
 r=subprocess.run(['docker','run','--detach','--name',name,'--network','host','--label','codex.epic=480','--label','codex.issue=486','--mount','type=bind,source='+str(config)+',target=/test.yaml,readonly','livekit/livekit-server:v1.9.7','--config','/test.yaml'],capture_output=True,text=True,check=True);container=r.stdout.strip();result['container']=container
 subprocess.run([py,'-c','import os;from pathlib import Path;from app.runtime_paths import resolve_runtime_paths;from app.runtime_data_root import initialize_runtime_data_root;r=Path('+repr(str(root))+');initialize_runtime_data_root(resolve_runtime_paths(os.environ,r),r)'],env=env,cwd=root/'backend',check=True)
 with (run/'backend.log').open('w') as log:
  backend=subprocess.Popen([py,'-X','faulthandler','-m','uvicorn','app.main:app','--host','127.0.0.1','--port','19495'],env=env,cwd=root/'backend',stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 result['backend_pid']=backend.pid
 for _ in range(100):
  if backend.poll() is not None:raise RuntimeError('backend startup exited '+str(backend.returncode))
  try:
   with urllib.request.urlopen('http://127.0.0.1:19495/openapi.json',timeout=1) as response:
    if response.status==200:break
  except Exception:time.sleep(.2)
 else:raise RuntimeError('backend startup timeout')
 env.update(DS_BACKEND_ORIGIN='http://127.0.0.1:19495',LIVEKIT_TEST_FRONTEND_URL='http://127.0.0.1:15195')
 profile={'reportSchemaVersion':1,'effectiveProfile':'integration-voice','readyGate':{'baseUrl':'http://127.0.0.1:19495','host':'127.0.0.1','port':19495},'dependencies':{},'capabilities':['text-chat-real','voice-chat-real']}
 for name,port in [('backend',19495),('frontend',15195),('ollama',11434),('voicevox',50021),('whisper',50022),('livekit',19586)]:
  profile['dependencies'][name]={'mode':'real','source':'managed' if name in ('backend','frontend') else 'external','baseUrl':'http://127.0.0.1:'+str(port)}
 profile['dependencies']['chroma']={'mode':'disabled','source':None}
 profile['derivedEnvironment']={k:env[k] for k in ['DS_ENVIRONMENT_ID','DS_DATA_DIR','RAG_ENABLED']};pp=run/'resolved-profile.json';pp.write_text(json.dumps(profile));env['DS_PROFILE_REPORT']=str(pp)
 with urllib.request.urlopen('http://127.0.0.1:19495/health/ready',timeout=20) as response:
  result['backend_readiness_status']=response.status
 with (run/'vite.log').open('w') as log:
  frontend=subprocess.Popen([node,'node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','15195','--strictPort'],cwd=root/'frontend',env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 for _ in range(100):
  if frontend.poll() is not None:raise RuntimeError('frontend exited')
  try:
   with urllib.request.urlopen('http://127.0.0.1:15195/',timeout=1) as response:
    if response.status==200:break
  except Exception:time.sleep(.2)
 else:raise RuntimeError('frontend startup timeout')
 cli=str(root/'frontend/node_modules/@playwright/test/cli.js')
 if True:
  configpath=root/'frontend/node_modules/.cache/ds486-voice.config.mjs';configpath.parent.mkdir(exist_ok=True)
  configpath.write_text("import {defineConfig,devices} from '@playwright/test';\nexport default defineConfig("+json.dumps({'testDir':str(root/'frontend/integration/voice-quality'),'outputDir':str(run/'voice-artifacts'),'fullyParallel':False,'workers':1,'retries':0,'maxFailures':1,'reporter':[['list'],['json',{'outputFile':str(run/'voice-results.json')}]],'use':{'baseURL':'http://127.0.0.1:15195'},'projects':[{'name':'isolated-voice/chromium','use':{'browserName':'chromium'}}]})+");\n")
  import threading
  stop=threading.Event()
  def sample():
   with (run/'backend-resources.jsonl').open('w') as output:
    while not stop.is_set():
     try:
      fields=pathlib.Path('/proc/'+str(backend.pid)+'/stat').read_text().split()
      output.write(json.dumps({'monotonic_ns':time.monotonic_ns(),'cpu_ticks':int(fields[13])+int(fields[14]),'rss_pages':int(fields[23]),'clock_ticks':os.sysconf('SC_CLK_TCK'),'page_bytes':os.sysconf('SC_PAGE_SIZE')})+'\n');output.flush()
     except FileNotFoundError:break
     stop.wait(.5)
  sampler=threading.Thread(target=sample,daemon=True);sampler.start()
  with (run/'browser-voice.log').open('w') as log:
   test=subprocess.run([node,cli,'test','--config',str(configpath),'livekit-quality.spec.ts'],env=env,cwd=root/'frontend',stdout=log,stderr=subprocess.STDOUT,timeout=1400)
  stop.set();sampler.join(timeout=2);result['browser_voice_exit']=test.returncode;print((run/'browser-voice.log').read_text()[-6500:],flush=True)
 result['backend_exit_before_cleanup']=backend.poll()
except Exception as e:result['exception']=str(e);print(type(e).__name__,str(e),flush=True)
finally:
 if frontend is not None and frontend.poll() is None:
  os.killpg(frontend.pid,signal.SIGTERM)
  try:frontend.wait(timeout=10)
  except subprocess.TimeoutExpired:os.killpg(frontend.pid,signal.SIGKILL);frontend.wait()
 if backend is not None and backend.poll() is None:
  os.killpg(backend.pid,signal.SIGTERM)
  try:backend.wait(timeout=10)
  except subprocess.TimeoutExpired:os.killpg(backend.pid,signal.SIGKILL);backend.wait()
 if container:
  logs=subprocess.run(['docker','logs',container],capture_output=True,text=True);(run/'livekit.log').write_text(logs.stdout+logs.stderr)
  subprocess.run(['docker','stop','--time','5',container],capture_output=True,check=True);subprocess.run(['docker','rm',container],capture_output=True,check=True)
 config.unlink(missing_ok=True)
 result['owned_resources_stopped']=True
 (run/'result.json').write_text(json.dumps(result,indent=2))
 print('RESULT',json.dumps(result),flush=True)
if result.get('browser_voice_exit') != 0 or 'exception' in result:
 raise SystemExit(1)
