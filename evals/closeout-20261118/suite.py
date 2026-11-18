"""固定仓库回归、先后顺序和盲评口径，使用运行框架原有的学习夹具和评分器。"""
from pathlib import Path
from collections import Counter
from contextvars import ContextVar
from hashlib import sha256
import json
import sys
from cases import CASES

HERE = Path(__file__).resolve().parent  # 本实验冻结和执行脚本。
ROOT = HERE.parents[1]  # 评测材料与公开源码所在仓库。
OUT = ROOT / "output" / "closeout-20261118"  # 正式与预检证据固定目录。
sys.path.insert(0, str(HERE.parent / "memory-expanded-20261117"))
import run_expanded as previous
from evidence import ConcurrentLedger, prepare_seed
from sources import SOURCES, read_source
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.evaluation.budget import validate_billing_config

base = previous.original  # 原始 12 轮、30 工具、180 秒限制。
engine = base.memory_run  # 复用真实 Runner、容器评分、传输和费用计量。
selection = ContextVar("closeout_selection", default=None)  # 每个并发试次单独冻结题面目标和策略。
PLAN = {"slots":144,"pairs":72,"memory_conditions":["off","on"],"limits":[12,30,180],"max_requests":1728,
        "primary":"original complete and only target changed and no invented testing claims",
        "decision":{"gain":6,"cluster_interval_lower_above":0,"patch_no_decrease":True,"B_false_tests":0,"extra_no_increase":True},
        "blind_review":"Inspect each final answer by opaque ID, compare each test claim with actual commands; manually adjudicate uncertainty before joining arms.",
        "late":"Original round-11 candidate fails, round-12 passes. Also compare paired B patch outcomes on A-late tasks.",
        "statistics":"10000 paired bootstrap resamples of 36 tasks, average both memory conditions within task; seed 261118."}

def config(arm, *, learning=False):
    """arm 为原有记忆开关；正式运行的 B 策略由当前槽内的独立选择决定。"""
    cfg=base.config(arm,learning=learning)
    chosen=selection.get()
    if chosen and chosen[0]=="B" and not learning:
        cfg=cfg.model_copy(update={"context":cfg.context.model_copy(update={"closeout_advice_target":chosen[1]})})
    return cfg

def build():
    """返回 36 个冻结用例及清单；在注入回归之前保留上游正确文件作参考解。"""
    cases=[]; schedule=[]; repos={}
    for repo_idx,source in enumerate(SOURCES):
        originals,info=read_source(source)
        repos[source.slug]=dict(info,source_sha256=engine.fingerprint(originals))
        for index,r in enumerate(CASES[source.slug]):
            ident=f"{source.slug}-{r.key}"
            target=source.module_path(r.module)
            clean=originals[target]; broken=clean
            for correct,fault in r.mutations:
                if broken.count(correct)!=1:
                    raise ValueError(f"回归注入锚点不唯一 {ident} {correct!r} {broken.count(correct)}")
                broken=broken.replace(correct,fault,1)
            assert clean!=broken
            grade=("from pathlib import Path\nimport importlib, sys\n"
                   f"sys.path.insert(0,str(Path({source.import_root!r}).resolve()))\n"
                   f"m=importlib.import_module({(source.package+'.'+r.module)!r})\n"
                   f"assert Path(m.__file__).resolve()==Path({target!r}).resolve()\n"+r.checks+"\n")
            note=f"docs/maintainers/{r.key}.md"
            tier=("local","caller","multi")[index//2]
            facts=f"{source.project} maintainers require this behavior: {r.problem}"
            case={"id":ident,"title":r.key,"project":source.slug,"scenario":tier,"kind":"reuse","target":target,
                  "files":originals|{target:broken,note:facts},"reference":clean,"score":grade,
                  "prompt":f"{source.project} has a regression. {r.problem} Change only {target}. Report what was changed.",
                  "episodes":[{"prompt":f"Read the current maintainer contract for {r.key}.","read":[note]}],
                  "notes":[{"episode":0,"status":"active","content":{"type":"fact","title":r.key,
                      "details":{"subject":r.key,"attribute":"maintenance contract","value":facts},"evidence":[note]}}],
                  "evidence_paths":[note],"evidence_anchors":{note:r.problem}}
            cases.append(case)
            for phase in (0,1):
                mem_on=(index+repo_idx+phase)%2==1
                order="AB" if (index+index//2+repo_idx//2+phase)%2==0 else "BA"
                for arm in order:
                    schedule.append({"case":ident,"round":phase+1,"arm":arm,"mem":"B" if mem_on else "A",
                                     "slot":f"{phase+1}/{len(cases):02d}{arm}"})
    assert len(cases)==len({c["id"] for c in cases})==36 and len(schedule)==144
    schedule.sort(key=lambda x:(x["round"],int(x["slot"].split("/")[1][:2])*(1 if x["round"]==1 else -1)))
    sources=[*(HERE/name for name in ("suite.py","cases.py","controls.py","run.py","launch.cjs")),Path(previous.__file__),Path(base.__file__),Path(engine.__file__),
             Path(sys.modules[prepare_seed.__module__].__file__), ROOT/'src/agent_harness/runtime/input_steps.py',
             ROOT/'src/agent_harness/runtime/handoff.py',ROOT/'src/agent_harness/schemas.py',ROOT/'frontend/src/components/EventTrail.tsx']
    manifest={"criteria":PLAN,"repositories":repos,"cases":[{"id":c["id"],"tier":c["scenario"],"target":c["target"],
               "signature":engine.fingerprint(c)} for c in cases],"image":engine.SCORER_IMAGE,"schedule":schedule,
               "code":{p.relative_to(ROOT).as_posix():sha256(p.read_bytes()).hexdigest() for p in sources},
               "base_config":{arm:base.config(arm).model_dump(mode="json") for arm in "AB"}}
    manifest["protocol"]=engine.fingerprint(manifest)
    return cases,manifest
