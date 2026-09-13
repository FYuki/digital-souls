"""固定合成ケースで指示・出力設計・モデルを比較する。最大20候補、出典検証は維持する。"""
import os,sys,json,time,hashlib,logging
from pathlib import Path
import argparse,subprocess
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--iteration",type=int,required=True,choices=range(1,21))
parser.add_argument("--output-dir",type=Path,required=True)
parser.add_argument("--design",choices=["production","compact"],default="compact")
parser.add_argument("--format",choices=["schema","plain"],default="schema")
parser.add_argument("--model",default="gemma4:e4b")
parser.add_argument("--cases",nargs="+",required=True)
args=parser.parse_args()
os.environ["EXPERIMENT_DESIGN"]=args.design
os.environ["EXPERIMENT_FORMAT"]=args.format
os.environ["EXPERIMENT_MODEL"]=args.model
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'backend'))
os.environ.update({"DS_ENVIRONMENT_ID":"test","PYTHON_DOTENV_DISABLED":"1","OLLAMA_BASE_URL":"http://127.0.0.1:11434","MEMORY_FORMATION_LLM_TIMEOUT_SECONDS":"180","MEMORY_FORMATION_TOTAL_TIMEOUT_SECONDS":"400"})
for name,out in [("CHAT",1024),("PRIVACY",512),("MEMORY_EXTRACTION",4096),("MEMORY_CONSOLIDATION",512)]:
 os.environ.setdefault("INFERENCE_TARGET_"+name,"ollama/"+os.environ.get("EXPERIMENT_MODEL","gemma4:e4b"))
 os.environ.setdefault("INFERENCE_TARGET_"+name+"_MAX_INPUT_TOKENS",str(36864-out))
 os.environ.setdefault("INFERENCE_TARGET_"+name+"_MAX_OUTPUT_TOKENS",str(out))
 os.environ.setdefault("INFERENCE_TARGET_"+name+"_TIMEOUT_SECONDS","180")
 os.environ.setdefault("INFERENCE_TARGET_"+name+"_OPTIONS_JSON",json.dumps({"temperature":0,"think":False}))
os.environ["INFERENCE_TARGET_EMBEDDING"]="ollama/nomic-embed-text:latest"
os.environ["INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS"]="8192"
from evals.episodic_quality import provider
from evals.episodic_quality.score import evaluate
from app.memory.inference_client import StructuredMemoryInferenceClient
dest=args.output_dir.resolve(); dest.mkdir(parents=True,exist_ok=False)
logging.basicConfig(filename=str(dest/"validation.log"),level=logging.WARNING)
if os.environ.get("EXPERIMENT_DESIGN") == "compact":
 from evals.episodic_quality.compact import CompactExtractor
 provider.ThreadEpisodeExtractor=CompactExtractor
original=StructuredMemoryInferenceClient.chat
if os.environ.get("EXPERIMENT_FORMAT") == "plain":
 from app.inference import InferenceMessage
 def original(self,messages,**kwargs):
  return self._router.generate_text(
   caller=self._caller,target=self._target,
   messages=tuple(InferenceMessage(m["role"],m["content"]) for m in messages),
   timeout_seconds=kwargs["timeout_seconds"]).text

active={}
def traced(self,messages,**kwargs):
 t=time.monotonic()
 row={"case":active["case"],"messages":messages,"schema":kwargs["json_schema"]}
 try:
  raw=original(self,messages,**kwargs); row["raw"]=raw; return raw
 except Exception as e:
  row["error_type"]=type(e).__name__; row["error"]=str(e); row["category"]=str(getattr(e,"category",None)); raise
 finally:
  row["seconds"]=time.monotonic()-t
  with (dest/"trace.jsonl").open("a") as f:f.write(json.dumps(row,ensure_ascii=False)+"\n")
StructuredMemoryInferenceClient.chat=traced
cases=[json.loads(x) for x in (root/"backend/evals/episodic_quality/cases.jsonl").read_text().splitlines()]
wanted=args.cases
cases=[c for c in cases if not wanted or c["id"] in wanted or c["vars"]["category"] in wanted]
if not cases or any(c.get("synthetic") is not True for c in cases):
 raise ValueError("only registered synthetic cases may be traced")
offered={c["id"] for c in cases}|{c["vars"]["category"] for c in cases}
if not set(wanted)<=offered:
 raise ValueError("unknown case or category")
manifest={"iteration":args.iteration,"commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip(),
"corpus_sha256":hashlib.sha256((root/"backend/evals/episodic_quality/cases.jsonl").read_bytes()).hexdigest(),
"design":os.environ.get("EXPERIMENT_DESIGN","production"),"format":os.environ.get("EXPERIMENT_FORMAT","schema"),"cases":[c["id"] for c in cases],"model":os.environ.get("EXPERIMENT_MODEL","gemma4:e4b"),"files":{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/"backend/app/memory/formation").glob("*.py")},"scope":"diagnostic direct production-provider calls; fixed truth; no cache"}
for source in [root/"backend/app/memory/formation/episodic_extractor.py",root/"backend/evals/episodic_quality/compact.py"]:
 (dest/source.name).write_bytes(source.read_bytes())
(dest/"manifest.json").write_text(json.dumps(manifest,indent=2))
results=[]
for c in cases:
 active["case"]=c["id"]
 result=json.loads(provider.call_api(c["vars"]["input_json"],{},{"vars":c["vars"]})["output"])
 passed=evaluate(result,c["vars"])
 row={"id":c["id"],"category":c["vars"]["category"],"pass":passed,"output":result}
 results.append(row)
 with (dest/"results.jsonl").open("a") as f:f.write(json.dumps(row,ensure_ascii=False)+"\n")
 print(c["id"],passed,result.get("error_type"),round(result["latency_seconds"],1),flush=True)

summary={"iteration":args.iteration,"correct":sum(r["pass"] for r in results),"total":len(results),
"scope":"固定部分集合の診断。全件受入ではない。",
"categories":{cat:{"correct":sum(r["pass"] for r in results if r["category"]==cat),
"total":sum(r["category"]==cat for r in results)} for cat in sorted({r["category"] for r in results})}}
(dest/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False),flush=True)
