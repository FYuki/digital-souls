"""IT2 for ADR 0026: real llama.cpp template acceptance and token counts. Prints counts only."""
import asyncio, json, sys
from pathlib import Path
from digital_souls_core.application import Inference
from digital_souls_core.character import load_characters
from digital_souls_core.contracts import CompletionInput
from digital_souls_core.provider import LiteLLMProvider
from digital_souls_core.prompt_builder import ByteTokenCounter
from digital_souls_core.contracts import Message

U=lambda t:{"role":"user","content":t}; A=lambda t:{"role":"assistant","content":t}; S=lambda t:{"role":"system","content":t}
CASES={
 "user_only":("miori",[U("こんばんは。自己紹介をお願いします。")]),
 "caller_system_mid":("miori",[S("合成試験：あなたは外部クライアントから呼ばれています。"),U("今日は晴れでした。"),A("晴れの日は気持ちがいいですね。"),U("夕方は何をしていましたか。")]),
 "post_history":("miori-phi",[U("好きな景色を教えてください。")]),
 "leading_assistant":("miori",[A("こんばんは、光織です。"),U("こんばんは。")]),
 "history_and_system_and_phi":("miori-phi",[S("合成試験の呼び出し側の指示です。"),U("一つ目の質問です。"),A("一つ目の答えです。"),U("二つ目の質問です。"),A("二つ目の答えです。"),U("三つ目の質問です。")]),
 "leading_assistant_long":("miori",[A("こんばんは、光織です。"),U("一つ目です。"),A("一つ目の答えです。"),U("二つ目です。")]),
 "tool_free_history_only_assistant_first":("miori-phi",[S("合成試験の指示です。"),A("最初の挨拶です。"),U("こんにちは。")]),
 "estimate_counter":("miori-est",[U("こんばんは。")]),
}
async def main(cfg, runs):
    chars=load_characters(Path(cfg)); prov=LiteLLMProvider(); inf=Inference(chars,prov)
    out=[]
    for name,(cid,msgs) in CASES.items():
        for r in range(runs):
            rec={"case":name,"run":r+1}
            try:
                p=await inf.prepare(cid,CompletionInput.model_validate({"messages":msgs,"max_completion_tokens":24,"temperature":0.0}),alias=False)
                rec["counted"]=p.usage.total_tokens
                rec["estimate"]=ByteTokenCounter()(tuple(Message.model_validate(m) for m in p.payload["messages"]))
                rec["roles"]=[m["role"] for m in p.payload["messages"]]
                res=await prov.complete(p.character.config.profile,p.payload)
                rec["prompt_tokens"]=(res.get("usage") or {}).get("prompt_tokens")
                rec["finish"]=res["choices"][0].get("finish_reason")
                rec["ok"]=True
            except Exception as e:
                rec["ok"]=False; rec["error"]=type(e).__name__+":"+str(getattr(e,"code",""))
            out.append(rec); print(json.dumps(rec,ensure_ascii=False),flush=True)
    json.dump(out,open(sys.argv[2],"w"),ensure_ascii=False,indent=1)
asyncio.run(main(sys.argv[1], int(sys.argv[3])))
