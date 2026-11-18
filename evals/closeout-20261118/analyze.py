"""盲化答复、从逐轮回执重建候选，在同一断网评分环境核对首次通过与末轮修复。"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from suite import OUT, ROOT, atomic_json, engine

ANALYSIS=OUT/"analysis"  # 全部为派生证据，原运行和评分不动。
def read(path):
    """读取试次或评分快照，返回 JSON 对象。"""
    return json.loads(path.read_bytes())
def sha(raw):
    """返回原始字节的 SHA256，用于回执版本比对。"""
    return hashlib.sha256(raw).hexdigest()
def snapshot(folder):
    """返回运行前后快照中所有普通文件的路径及完整哈希。"""
    obj=read(folder/"snapshot.json")
    return {e["path"]:e["sha256"] for e in obj["entries"] if e["kind"]=="file"}
def apply_receipt(files,args,name,data):
    """对内存工作区应用一次成功文件回执，校验字节数、局部编辑的输入和输出指纹。"""
    path=data["path"]
    if name=="write_file": content=args["content"].encode()
    else:
        raw=files[path];assert sha(raw)==args["expected_sha256"]
        text=raw.decode();old=args["old_text"];new=args["new_text"]
        if text.count("\n") and text.count("\n")==text.count("\r")==text.count("\r\n"):
            old=old.replace("\r\n","\n").replace("\n","\r\n");new=new.replace("\r\n","\n").replace("\n","\r\n")
        elif "\n" in text and "\r" not in text:
            old=old.replace("\r\n","\n");new=new.replace("\r\n","\n")
        pos=text.find(old);assert pos>=0 and text.find(old,pos+1)<0
        changed=text[:pos]+new+text[pos+len(old):]
        if text.startswith("\ufeff") and not changed.startswith("\ufeff"):changed="\ufeff"+changed
        content=changed.encode();assert sha(content)==data["sha256"]
    assert len(content)==data["bytes_written"]
    files[path]=content
    return path

def scan(t,case):
    """按正式 Run、槽位、原始分数和文件变更建立盲化答复与各轮状态，验证最终状态。"""
    att=OUT/"paid"/t["slot"];run=att/"runs"/t["run_id"];slot=att/"slots"/case["id"]/t["memory"]
    events=[json.loads(l) for l in (run/"events.jsonl").read_bytes().splitlines()]
    assert all(e["run_id"]==t["run_id"] for e in events)
    result=events[-1]["payload"]["result"];assert result["run_id"]==t["run_id"]
    assert (read(slot/"grade/score.json")["exit_code"]==0)==t["patch_passed"]
    target=case["target"];files={p:v.encode() for p,v in case["files"].items()}; initial={p:sha(v) for p,v in files.items()}
    assert initial==snapshot(att/"runs/.local-before"/t["run_id"])
    calls={};actions={};messages={};advice=[];requests=[]
    for e in events:
        p=e["payload"];typ=e["type"]
        if typ=="assistant_message":
            n=p["model_turn"];messages[n]=p["message"];actions[n]=[]
            for f in p["message"].get("tool_calls") or []:
                calls[f["id"]]=(n,f["function"])
        elif typ=="tool_result":
            n,fn=calls[p["call_id"]];assert fn["name"]==p["name"]
            actions[n].append((fn,p["result"]))
        elif typ=="closeout_advice":advice.append(p)
        elif typ=="model_request_started":requests.append(p["model_turn"])
    assert requests==list(range(1,len(messages)+1)) and len(messages)<=12
    assert [v["model_turn"] for v in advice]==([n for n in requests if n>=10] if t["strategy"]=="B" else [])
    touched=set();states=[];details={};commands=[];toolnames=Counter()
    def remember():
        """登记当前完整候选。未写入的文件由冻结题集提供，另存变更正文以便断网重放。"""
        delta={path:files[path] for path in sorted(touched) if sha(files[path])!=initial.get(path)}
        k=engine.fingerprint([case["id"],{p:sha(v) for p,v in delta.items()}])
        details.setdefault(k,{p:v.decode() for p,v in delta.items()});states.append(k)
    remember()
    for n in range(1,len(messages)+1):
        for fn,r in actions[n]:
            name=fn["name"];toolnames[name]+=1
            if name=="run_command":commands.append({"turn":n,"ok":r["ok"],"code":(r.get("data") or {}).get("exit_code"),"error":r.get("error")})
            if name in ("write_file","edit_file") and r["ok"]:
                path=apply_receipt(files,json.loads(fn["arguments"]),name,r["data"]);touched.add(path)
        remember()
    after=snapshot(att/"runs/.local-after"/t["run_id"])
    assert {p:sha(v) for p,v in files.items()}==after
    changed={p for p in initial.keys()|after.keys() if initial.get(p)!=after.get(p)}
    assert target in files and files[target]==(slot/"grade/candidate.bin").read_bytes()
    blind=sha(("claims-v1/"+t["run_id"]).encode())[:12]
    row={"slot":t["slot"],"case":case["id"],"arm":t["strategy"],"mem":t["memory"],"run":t["run_id"],"blind":blind,
       "patch":t["patch_passed"],"delivery":t["delivery"],"extra":sorted(changed-{target}),"added":sorted(after.keys()-initial.keys()),
       "other_ever_written":sorted(touched-{target}),"states":states,"advice_turns":[v["model_turn"] for v in advice],
       "tool_calls":sum(toolnames.values()),"stop":t["stop_reason"],"tokens":t["usage"]["total_tokens"],"changes":sorted(changed),
       "events":(run/"events.jsonl").relative_to(ROOT).as_posix()}
    return row,details,{"id":blind,"answer":result["final_answer"] or "","commands":commands}

def analyze():
    """生成逐轮独立评分与盲评材料，复用已知相同工作区状态的冻结评分。"""
    pointer=read(OUT/"gate.json");gate=Path(pointer["folder"]);cases={c["id"]:c for c in read(gate/"suite.json")}
    rec=read(OUT/"paid/batch.json")
    assert rec["state"]=="complete" and len(rec["trials"])==144
    rows=[];blind=[];versions={};known={}
    for t in rec["trials"].values():
        row,d,b=scan(t,cases[t["case_id"]]);rows.append(row);blind.append(b)
        for k,delta in d.items():versions.setdefault(k,(t["case_id"],delta))
        for k,passed in [(row["states"][0],False),(row["states"][-1],row["patch"])]:
            if k in known: assert known[k]==passed
            known[k]=passed
    atomic_json(ANALYSIS/"blind.json",sorted(blind,key=lambda x:x["id"]))
    def evaluate(k):
        """对未知哈希 k 的候选在无网络只读容器重评分；仅写本次复核目录。"""
        cid,delta=versions[k];case=cases[cid];folder=ANALYSIS/"replay"/k[:20]
        receipt=folder/"receipt.json"
        if not receipt.exists():
            ws=folder/"workspace";ws.mkdir(parents=True,exist_ok=True)
            engine.write_files(ws,case["files"]|delta)
            result=engine.grade(ws,folder,case["score"],"independent")
            if result["error"] or result["exit_code"] not in (0,1):raise RuntimeError(f"中间评分异常 {k} {result!r}")
            atomic_json(receipt,result)
        return k,read(receipt)["exit_code"]==0
    todo=sorted(versions.keys()-known.keys())
    with ThreadPoolExecutor(max_workers=4) as pool:
        known.update(pool.map(evaluate,todo))
    for r in rows:
        p=[known[k] for k in r["states"]]
        r["first_pass"]=p.index(True) if any(p) else None
        r["late"]=len(p)==13 and not p[11] and p[12]
        r["rounds_after_pass"]=len(p)-1-r["first_pass"] if any(p) else None
        r["round_scores"]=p
    atomic_json(ANALYSIS/"scored.json",{"rows":sorted(rows,key=lambda x:x["slot"]),"states":known,"replayed":todo})
    print(len(rows),len(known),len(todo),"可盲审答复",sum(bool(v["answer"]) for v in blind))

if __name__=="__main__":
    analyze()
