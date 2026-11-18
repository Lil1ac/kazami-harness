"""用固定模型回执走真实文件执行路径，核对末轮编辑、失效事实和界面字段。"""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from suite import base, engine
from agent_harness.runtime.input_steps import add_closeout_advice


class Scripted:
    """按回合交替读写工作区，记录实际模型收到的提示与工具定义，始终不联网。"""

    def __init__(self, place, flow):
        """place 为隔离工作区，flow 选择提前完成、末轮首次编辑或先写错后修的固定路径。"""
        self.place=place  # 仅用于模拟其他操作者更改已写文件。
        self.flow=flow  # 本次固定场景。
        self.turns=[]  # 按真实请求顺序保存工具名和提示事实。

    async def complete(self, messages, tools):
        """根据收到的消息和工具返回确定性响应，末轮编辑走真实工具与正常限额终态。"""
        n=len(self.turns)+1
        notices=[msg["content"] for msg in messages if msg["role"]=="system" and msg["content"].startswith("宿主预算提醒。")]
        assert len(notices)<=1
        facts=json.loads(notices[0].split("\n")[-1]) if notices else None
        self.turns.append({"facts":facts,"tools":[v["function"]["name"] for v in tools]})
        name,args="read_file",{"path":"main.txt"}
        if self.flow in {"early","stale","partial"} and n==8:
            name,args="write_file",{"path":"main.txt","content":"partial" if self.flow=="partial" else "done","overwrite":True}
        if self.flow=="extra" and n==9:
            name,args="write_file",{"path":"extra.py","content":""}
        if self.flow=="denied" and n==9:
            name,args="run_command",{"argv":["python","-V"]}
        if self.flow=="stale" and n==9:
            (self.place/"main.txt").write_text("changed elsewhere",encoding="utf-8")
        if n==12 and self.flow in {"late","partial"}:
            name,args="write_file",{"path":"main.txt","content":"done","overwrite":True}
        usage={"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}
        if n==12 and self.flow not in {"late","partial"}:
            return engine.ModelResponse({"role":"assistant","content":"已核对当前文件，未运行测试。"},[],"stop","offline",usage=usage)
        call=engine.ToolCall(f"c{n}",name,json.dumps(args))
        message={"role":"assistant","content":None,"tool_calls":[{"id":call.call_id,"type":"function","function":{"name":name,"arguments":call.arguments_json}}]}
        return engine.ModelResponse(message,[call],"tool_calls","offline",usage=usage)


async def verify(folder):
    """在 folder 中走两臂真实 Runner；逐轮验证注入边界、末轮写入、越范围回执与陈旧证据。"""
    rows=[]
    for flow in ("early","late","partial","denied","extra","stale"):
        for arm in "AB":
            work=folder/flow/arm/"work";work.mkdir(parents=True)
            (work/"main.txt").write_text("old",encoding="utf-8")
            cfg=base.config("A")
            if arm=="B":cfg=cfg.model_copy(update={"context":cfg.context.model_copy(update={"closeout_advice_target":"main.txt"})})
            manifest=engine.RunManifest(run_id=uuid4(),task=engine.prepare_task("Change only main.txt.",work,python_executable="python"),config=cfg)
            model=Scripted(work,flow);runs=work.parent/"runs"
            trial=await engine.run_task(manifest,runs,"offline",executor=engine.EvalTools(work,runs,cfg),model_client=model)
            events=engine.RunStore.read(runs,str(manifest.run_id)).events
            notices=[e.payload for e in events if e.type=="closeout_advice"]
            actual=[r["facts"] for r in model.turns if r["facts"]]
            assert notices==actual and [r["model_turn"] for r in actual]==([10,11,12] if arm=="B" else [])
            assert all("write_file" in r["tools"] and "edit_file" in r["tools"] for r in model.turns)
            assert not trial.result.http_attempts and len(model.turns)==12
            if arm=="B":
                assert all(r["current_target_write"]==(flow in {"early","partial"}) for r in actual)
                assert all(r["other_writes"]==(["extra.py"] if flow=="extra" else []) for r in actual)
                assert all(r["self_test"]=="unavailable" for r in actual)
            if flow in {"partial","late"}:assert trial.result.stop_reason=="max_model_turns" and (work/"main.txt").read_text()=="done"
            else:assert trial.result.execution_status=="completed"
            if flow=="denied":assert any(e.type=="tool_result" and (e.payload["result"].get("error") or {}).get("code")=="unknown_tool" for e in events)
            rows.append({"flow":flow,"strategy":arm,"id":str(manifest.run_id),"events":notices,"http":0})
    # 待审批时执行层正常暂停；即便提前进入提示投影，也不得把待审批写入描述为已经提交。
    ctx=SimpleNamespace(manifest=SimpleNamespace(config=cfg,delegation=None),available=lambda _:1,
                        result=SimpleNamespace(permissions=SimpleNamespace(pending={"id":"pending"})))
    add_closeout_advice(SimpleNamespace(context=ctx,turn=SimpleNamespace(input=object(),delegated=False)))
    return rows
