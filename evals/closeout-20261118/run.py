"""冻结单文件仓库题，零模型预检通过后在官方端点运行成对试次，逐槽落盘。"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import msvcrt
from pathlib import Path
import sys
from unittest.mock import patch
from suite import OUT, PLAN, HERE, ConcurrentLedger, atomic_json, base, build, config, engine, prepare_seed, selection, validate_billing_config
from controls import verify


def read(path):
    """读取 path 所指冻结回执，返回解析后 JSON 数据。"""
    return json.loads(path.read_bytes())


def score_controls(case,folder):
    """离线独立容器验证负起点、正参考及多处修复的各个残余缺陷，返回评分回执。"""
    result=engine.scoring_preflight(case,folder)
    if case["scenario"]=="multi":
        from cases import CASES
        spec=next(t for t in CASES[case["project"]] if t.key==case["title"])
        sub=folder/"scoring-preflight"/case["id"]
        for i,(good,bad) in enumerate(spec.mutations):
            candidate=case["reference"].replace(good,bad,1)
            engine.write_files(sub/"workspace",{case["target"]:candidate})
            verdict=engine.grade(sub/"workspace",sub,case["score"],f"missing-{i}")
            if verdict["exit_code"]!=1:raise ValueError(f"{case['id']} 缺少第 {i+1} 处修复仍通过")
            result[f"missing-{i}"]=verdict
    return result


async def preflight(cases,manifest):
    """生成只读冻结清单，在零供应商请求条件下验证所有评分器、学习与收尾分支。"""
    folder=OUT/"preflight"/datetime.now().strftime("%H%M%S%f")
    folder.mkdir(parents=True)
    atomic_json(folder/"manifest.json",manifest)
    atomic_json(folder/"suite.json",cases)
    report={"state":"checking","protocol":manifest["protocol"],"scores":{},"seeds":{},"errors":{},"model_requests":0}
    atomic_json(folder/"gate.json",report)
    workers=asyncio.Semaphore(4)
    async def check(i,case):
        """在独立目录检查第 i 题的模型无关正负控制及学习指纹。"""
        async with workers:
            try:
                receipt=await asyncio.to_thread(score_controls,case,folder)
                seed=await prepare_seed(case,"A",folder/"l"/str(i))
                report["scores"][case["id"]]=receipt
                report["seeds"][case["id"]]=seed["fixture_sha256"]
            except Exception as error:
                report["errors"][case["id"]]=str(error)
            atomic_json(folder/"gate.json",report)
    try:
        report["flow_controls"]=await verify(folder/"flows")
    except Exception as error:
        report["errors"]["flow_controls"]=repr(error)
    await asyncio.gather(*(check(i,c) for i,c in enumerate(cases)))
    report["state"]="passed" if len(report["scores"])==36 and not report["errors"] else "failed"
    atomic_json(folder/"gate.json",report)
    if report["state"]=="passed":
        atomic_json(OUT/"gate.json",{"folder":str(folder),"protocol":manifest["protocol"],"receipt":engine.fingerprint(report)})
    print(folder,report["state"],report["errors"],flush=True)
    return report["state"]=="passed"


async def paid(cases,manifest,key):
    """核对冻结指纹和请求上限，按题顺序运行两种策略，保留中断槽的原件以供恢复。"""
    pointer=read(OUT/"gate.json");gate=read(Path(pointer["folder"])/"gate.json")
    if not (gate["state"]=="passed" and gate["protocol"]==manifest["protocol"]==pointer["protocol"]
            and gate["model_requests"]==0 and engine.fingerprint(gate)==pointer["receipt"]):
        raise ValueError("本批预检证据不匹配")
    directory=OUT/"paid";directory.mkdir(exist_ok=True)
    with (directory/"campaign.lock").open("a+b") as lock:
        lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        batch=directory/"batch.json"
        rec=read(batch) if batch.exists() else {"protocol":manifest["protocol"],"trials":{},"active":{}}
        if rec["protocol"]!=manifest["protocol"] or rec["active"]:
            raise RuntimeError("已有不同协议或未对账的中断槽位，请核对原运行")
        def save():
            """原子保存本次账本、已完成槽及正在执行的路径。"""
            atomic_json(batch,rec)
        for mem in "AB":validate_billing_config(config(mem))
        ledger=ConcurrentLedger(rec,save,1728)
        if ledger.stop_reason or ledger.pending:raise RuntimeError("有未对账的模型请求")
        rec["state"]="running";save()
        by_id={c["id"]:c for c in cases}
        workers=asyncio.Semaphore(6)
        async def pair(slots):
            """同题同记忆的 A/B 顺序执行，独立题并发，不重跑已保存的槽位。"""
            async with workers:
                for spec in slots:
                    slot=spec["slot"]
                    if slot in rec["trials"] or rec.get("error") or ledger.stop_reason:continue
                    case=by_id[spec["case"]];att=directory/slot;mem=spec["mem"]
                    rec["active"][slot]=spec;save()
                    token=selection.set((spec["arm"],case["target"]))
                    try:
                        seed=await prepare_seed(case,mem,att)
                        if seed["fixture_sha256"]!=gate["seeds"][case["id"]]:raise ValueError("种子证据与预检不匹配")
                        row=await engine.trial(case,mem,att,key=key,guard=ledger,real=True)
                        row.update(strategy=spec["arm"],memory=mem,slot=slot)
                        atomic_json(att/"outcome.json",row)
                        rec["trials"][slot]=row
                        if row["patch_passed"] is None or not row["usage_complete"]:
                            rec["error"]="grading_or_usage"
                        rec["active"].pop(slot)
                        print(f"{len(rec['trials'])}/144 {slot} {row['stop_reason']}",flush=True)
                    except Exception as error:
                        rec["error"]=f"{slot}: {type(error).__name__} {error}"
                    finally:
                        selection.reset(token);save()
        with patch.object(engine,"config",config):
            for phase in (1,2):
                slots=[s for s in manifest["schedule"] if s["round"]==phase]
                await asyncio.gather(*(pair(slots[i:i+2]) for i in range(0,len(slots),2)))
        rec["state"]="complete" if len(rec["trials"])==144 and not rec.get("error") and not ledger.stop_reason else "stopped"
        save();print(rec["state"],rec.get("error"),ledger.stop_reason,flush=True)
        return rec["state"]=="complete"


async def main():
    """运行指定阶段；正式阶段只从匿名标准输入读取凭据，不打印、不写入结果。"""
    parser=argparse.ArgumentParser();parser.add_argument("--real",action="store_true");args=parser.parse_args()
    cases,manifest=build()
    if not args.real:return await preflight(cases,manifest)
    key=sys.stdin.readline(2048).strip()
    if not engine.valid_model_key(key):raise ValueError("官方凭据不可用")
    return await paid(cases,manifest,key)

if __name__=="__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
