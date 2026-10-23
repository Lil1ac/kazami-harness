"""验证会话持久幂等、历史配对和普通文件快照的真实边界行为。"""

from pathlib import Path
import json
from uuid import uuid4

import pytest

from agent_harness.schemas import RunConfig, RunManifest, RunResult, TaskSpec
from agent_harness.sessions import SessionError, SessionStore, history_messages, payload_digest
from agent_harness.snapshots import FileSnapshotStore, SnapshotError
from agent_harness.store import RunStore


def complete_history(tmp_path: Path):
    """写入一次含真实工具证据的完整历史，返回读取结果；不调用外部模型或工具。"""
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt='first', workspace=workspace), config=RunConfig())
    store = RunStore.create(tmp_path / 'records', manifest)
    store.append('run_started', {'messages':[{'role':'system','content':'old workspace'}, {'role':'user','content':'first'}], 'tools':[], 'conversation_start':1})
    message = {'role':'assistant','content':None,'reasoning_content':'原始推理字段','tool_calls':[{'id':'call-1','type':'function','function':{'name':'read_file','arguments':'{}'}}]}
    store.append('assistant_message', {'message':message})
    store.append('tool_result', {'call_id':'call-1','name':'read_file','result':{'ok':True,'data':{'content':'内容'},'error':None,'truncated':False}})
    store.append('assistant_message', {'message':{'role':'assistant','content':'done'}})
    store.finalize(RunResult(run_id=manifest.run_id, execution_status='completed', model_turns=2, tool_calls=1))
    return RunStore.read(tmp_path / 'records', str(manifest.run_id))


def test_history_removes_old_system_and_preserves_exact_tool_content(tmp_path: Path):
    """继承输入没有旧宿主前缀，保留供应商字段和完整可回传工具JSON。"""
    history = complete_history(tmp_path)
    messages = history_messages(history)
    assert [item['role'] for item in messages] == ['user','assistant','tool','assistant']
    assert messages[1]['reasoning_content'] == '原始推理字段'
    assert json.loads(messages[2]['content'])['data']['content'] == '内容'
    messages[0]['content'] = 'changed'
    assert history_messages(history)[0]['content'] == 'first'


@pytest.mark.parametrize('damage', ['missing_result','bad_result','bad_call_id','duplicate_call','unknown_role'])
def test_damaged_history_is_rejected_as_controlled_error(tmp_path: Path, damage: str):
    """残缺或形状异常工具历史统一拒绝，不能触发TypeError或把错误消息当作有效上下文。"""
    history = complete_history(tmp_path)
    tool = next(event for event in history.events if event.type == 'tool_result')
    assistant = next(event for event in history.events if event.type == 'assistant_message')
    if damage == 'missing_result':
        history.events.remove(tool)
    elif damage == 'bad_result':
        tool.payload['result'] = None
    elif damage == 'bad_call_id':
        tool.payload['call_id'] = ['invalid']
    elif damage == 'duplicate_call':
        assistant.payload['message']['tool_calls'] *= 2
    else:
        assistant.payload['message']['role'] = 'system'
    with pytest.raises(SessionError, match='历史'):
        history_messages(history)


def test_session_intents_persist_and_require_latest_parent(tmp_path: Path):
    """重建存储对象仍找到原请求；同编号不同载荷、旧父运行均拒绝，失败准备不推进会话。"""
    root, child, request = uuid4(), uuid4(), uuid4()
    store = SessionStore(tmp_path / 'records')
    digest = payload_digest({'prompt':'second'})
    store.begin(root, request, root, child, digest, 'second')
    assert SessionStore(tmp_path / 'records').lookup(root, request, digest).state == 'preparing'
    with pytest.raises(SessionError, match='不同内容'):
        store.lookup(root, request, payload_digest({'prompt':'other'}))
    store.commit(root, request)
    assert store.read(root).run_ids == [root, child]
    with pytest.raises(SessionError, match='最新'):
        store.begin(root, uuid4(), root, uuid4(), digest, 'stale')
    failed = uuid4()
    store.begin(root, failed, child, uuid4(), digest, 'next')
    store.fail(root, failed, 'prepare_failed', '尚未执行')
    assert store.read(root).run_ids == [root, child]
    assert store.lookup(root, failed, digest).state == 'failed'


def test_binary_readonly_and_empty_directory_survive_file_snapshot(tmp_path: Path):
    """实际二进制、只读文件和空目录完整继承，来源与副本相互独立。"""
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'empty').mkdir()
    file = workspace / 'binary.bin'
    file.write_bytes(b'\0\xff\r\n')
    file.chmod(0o444)
    store = FileSnapshotStore(tmp_path / 'records')
    identifier = uuid4()
    store.capture(identifier, workspace)
    assert store.info(identifier)['status'] == 'ready'
    destination = tmp_path / 'child'
    store.restore(identifier, workspace, destination)
    assert (destination / 'binary.bin').read_bytes() == b'\0\xff\r\n'
    assert (destination / 'empty').is_dir()
    assert (destination / 'binary.bin').stat().st_mode == file.stat().st_mode


@pytest.mark.parametrize('damage', ['content','traversal','identity'])
def test_file_snapshot_damage_is_rejected(tmp_path: Path, damage: str):
    """正文损坏、清单越界或来源身份替换时，拒绝继承且保留原文件证据。"""
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'file.txt').write_bytes(b'original')
    store = FileSnapshotStore(tmp_path / 'records')
    identifier = uuid4()
    store.capture(identifier, workspace)
    directory = store.root / str(identifier)
    if damage == 'content':
        (directory / 'files/file.txt').write_bytes(b'tampered')
    elif damage == 'traversal':
        path = directory / 'snapshot.json'
        info = json.loads(path.read_text(encoding='utf-8'))
        info['entries'][0]['path'] = '../outside'
        info['tree_sha256'] = payload_digest(info['entries'])
        path.write_text(json.dumps(info), encoding='utf-8')
    else:
        moved = tmp_path / 'old-workspace'
        assert moved.resolve().is_relative_to(tmp_path.resolve()) and workspace.resolve().is_relative_to(tmp_path.resolve())
        workspace.rename(moved)
        workspace.mkdir()
        (workspace / 'file.txt').write_bytes(b'original')
    with pytest.raises(SnapshotError):
        store.restore(identifier, workspace, tmp_path / 'child')
    assert (workspace / 'file.txt').read_bytes() == b'original'
    assert not (tmp_path / 'outside').exists()
