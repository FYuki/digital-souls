
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import http.client,json,time,sys
root=Path(sys.argv[1])
class Handler(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*args):pass
 def do_GET(self):self.forward()
 def do_POST(self):self.forward()
 def forward(self):
  started=time.monotonic()
  body=self.rfile.read(int(self.headers.get('Content-Length','0')))
  fault=(root/'fail-readiness').exists() and self.path=='/health/ready'
  if fault:status=503;payload=b'{"status":"not_ready"}';ctype='application/json'
  else:
   conn=http.client.HTTPConnection('127.0.0.1',50024,timeout=120)
   try:
    headers={k:v for k,v in self.headers.items() if k.lower() not in ['host','connection','transfer-encoding','content-length']}
    conn.request(self.command,self.path,body=body,headers=headers)
    response=conn.getresponse();status=response.status;payload=response.read();ctype=response.getheader('Content-Type','application/octet-stream')
   except Exception:status=502;payload=b'{"status":"proxy_error"}';ctype='application/json'
   finally:conn.close()
  with (root/'proxy-events.jsonl').open('a') as log:
   log.write(json.dumps({'kind':'readiness' if self.path=='/health/ready' else 'speech' if self.path.endswith('/audio/speech') else 'other','injected':fault,'status':status,'elapsed_ms':(time.monotonic()-started)*1000})+'\n')
  self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(payload)));self.end_headers()
  try:self.wfile.write(payload)
  except (BrokenPipeError,ConnectionResetError):pass
ThreadingHTTPServer(('127.0.0.1',50526),Handler).serve_forever()
