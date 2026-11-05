"""测试工作流：通过统一作业接纳入口建立 Run，再把真实 RunView 交给领域断言。"""

from uuid import uuid4
import httpx


async def _view(client, response):
    """将接纳 response 关联到真实运行视图；未建立 Run 时保留作业响应及错误，返回 HTTP 响应。"""
    if response.status_code != 202:
        return response
    job = response.json()
    if job["status"] == "needs_review" or (job["status"] == "failed" and job.get("error_code")):
        return httpx.Response(job.get("error_status", 409), json={"error": {"code": job.get("error_code", "job_needs_review"), "message": job["error"]}}, request=response.request)
    if job["status"] in {"queued", "preparing"}:
        return response
    view = await client.get('/api/runs/' + job['run_id'])
    return httpx.Response(202, json=view.json(), request=response.request)


async def submit_run(client, *, json: dict, **options):
    """通过 client 接纳 json 首轮；options 为同源头等 HTTP 参数，返回真实 RunView 接纳响应。"""
    return await _view(client, await client.post('/api/jobs', json={"request_id": str(uuid4()), "create": json}, **options))


async def submit_followup(client, path: str, *, json: dict, **options):
    """将测试 path 中的父编号及 json 送入统一作业入口；options 原样传 HTTP，返回真实运行视图。"""
    parent = path.split('/')[3]
    return await _view(client, await client.post('/api/jobs', json={"request_id": json['request_id'], "parent_run_id": parent, "followup": json}, **options))
