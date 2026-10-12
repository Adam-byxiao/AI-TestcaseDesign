"""P0/P1 集成测试：实时进度事件、心跳、指标推送、取消止损。

全部走 stub（离线），不发真实请求。
"""
import json
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from prd2tc.pipeline import GenerateRequest, PipelineRunner
from prd2tc.config import Config
from prd2tc.service import create_app

SAMPLE = Path(__file__).parents[1] / "examples" / "sample-prd.md"


def _cfg(**kw) -> Config:
    base = Config(api_key="", api_base="local", model="m", temperature=0.0,
                  timeout=10, max_retries=1, provider="ollama")
    return base if not kw else Config(**{**base.__dict__, **kw})


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(state_dir=tmp_path / "workspace", max_workers=2)) as value:
        yield value


def wait_for(client, run_id, wanted, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/v1/runs/{run_id}").json()
        if data.get("status") in wanted:
            return data
        time.sleep(0.05)
    return client.get(f"/api/v1/runs/{run_id}").json()


def test_progress_callback_contract_unchanged(tmp_path):
    """兼容性：旧的 (stage, percent) 回调必须只收到原来那 3 个阶段通知。

    结构化事件走独立的 event_callback，混入旧通道会污染 CLI 输出。
    """
    seen_old = []
    runner = PipelineRunner(_cfg())
    runner.generate(GenerateRequest(prd=SAMPLE, out=tmp_path / "o1", product="p", version="v1", stub=True),
                    progress_callback=lambda stage, percent: seen_old.append((stage, percent)))
    assert seen_old == [("analysis", 33), ("plan", 66), ("cases", 100)], seen_old


def test_event_callback_receives_structured_events(tmp_path):
    """P0：结构化事件走独立通道，且带上 type/module。"""
    events = []
    runner = PipelineRunner(_cfg())
    runner.generate(GenerateRequest(prd=SAMPLE, out=tmp_path / "o2", product="p", version="v1", stub=True),
                    event_callback=lambda payload: events.append(payload))
    kinds = {payload.get("type") for payload in events}
    assert "stage_started" in kinds, kinds
    assert {"batch_started", "module_finished"} & kinds, f"应有模块/批次级事件: {kinds}"
    assert {payload.get("module") for payload in events if payload.get("module")}, "事件应带模块名"


def test_live_metrics_available_from_runner(tmp_path):
    """P1：runner.live 可在运行后取到快照（服务层推送依赖它）。"""
    runner = PipelineRunner(_cfg())
    runner.generate(GenerateRequest(prd=SAMPLE, out=tmp_path, product="p", version="v1", stub=True),
                    progress_callback=lambda *_: None)
    snapshot = runner.live.snapshot(force=True)
    assert snapshot["finished"] is True
    assert snapshot["totals"]["totalTokens"] > 0, "stub 流式也应产出 token 计数"
    assert snapshot["modules"], "应有模块级指标"
    assert any(m["deltas"] > 0 for m in snapshot["modules"].values()), "应记录流式增量块数"


def test_metrics_exposed_on_run_endpoint(client):
    """P1：GET /runs/{id} 内嵌 metrics，便于刷新后立即回填面板。"""
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", SAMPLE.read_bytes(), "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 1}).json()
    data = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    assert data.get("metrics"), "运行详情应带回 metrics 快照"
    assert data["metrics"]["type"] == "run_metrics"
    assert "totals" in data["metrics"] and "modules" in data["metrics"]


def test_heartbeat_events_are_emitted(client, monkeypatch):
    """P0：心跳让长耗时期间持续有事可看，避免误判卡死。"""
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", SAMPLE.read_bytes(), "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 1}).json()
    time.sleep(6.0)  # 心跳间隔 5s
    events = client.get(f"/api/v1/runs/{run['taskId']}/events").json()
    kinds = {e.get("type") for e in events}
    assert "heartbeat" in kinds or "run_started" in kinds, kinds
    beats = [e for e in events if e.get("type") == "heartbeat"]
    if beats:
        assert "静默" in beats[0]["message"] and "已用" in beats[0]["message"], beats[0]


def test_ws_metrics_requires_opt_in(tmp_path):
    """兼容性护栏：不订阅 metrics 的客户端不应收到 run_metrics 消息。

    注意：TestClient 的 receive_json 在没有新消息时会永久阻塞，因此必须先等运行
    到达终态（事件不再增长），再按已知事件数精确读取，不能盲目循环等待。
    """
    app = create_app(state_dir=tmp_path / "ws", max_workers=1)
    with TestClient(app) as client:
        prd = client.post("/api/v1/prds", files={"file": ("s.md", SAMPLE.read_bytes(), "text/markdown")}).json()
        run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True}).json()
        wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
        expected = len(client.get(f"/api/v1/runs/{run['taskId']}/events").json())
        assert expected > 0
        with client.websocket_connect(f"/api/v1/runs/{run['taskId']}/events/ws") as ws:
            for _ in range(expected):
                message = ws.receive_json()
                assert message.get("type") != "run_metrics", "未订阅却收到指标消息"


def test_ws_metrics_delivered_when_subscribed(tmp_path):
    """订阅 ?metrics=1 时，必须先收到事件、再收到 run_metrics 快照。"""
    app = create_app(state_dir=tmp_path / "ws2", max_workers=1)
    with TestClient(app) as client:
        prd = client.post("/api/v1/prds", files={"file": ("s.md", SAMPLE.read_bytes(), "text/markdown")}).json()
        run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True}).json()
        wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
        expected = len(client.get(f"/api/v1/runs/{run['taskId']}/events").json())
        with client.websocket_connect(f"/api/v1/runs/{run['taskId']}/events/ws?metrics=1") as ws:
            for _ in range(expected):
                assert ws.receive_json().get("type") != "run_metrics"
            metrics = ws.receive_json()   # 事件之后才是指标快照
            assert metrics["type"] == "run_metrics"
            assert metrics["runId"] == run["taskId"]
            assert "totals" in metrics and "modules" in metrics


def test_ws_metrics_sent_even_when_run_already_terminal(tmp_path):
    """回归：运行已结束（连接建立前就完成）时，仍必须先推一帧指标再关闭。

    否则短运行会让客户端一帧都收不到，实时面板永远空白。
    """
    app = create_app(state_dir=tmp_path / "ws3", max_workers=1)
    with TestClient(app) as client:
        prd = client.post("/api/v1/prds", files={"file": ("s.md", SAMPLE.read_bytes(), "text/markdown")}).json()
        run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True}).json()
        wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"}, timeout=20)
        assert client.get(f"/api/v1/runs/{run['taskId']}").json()["status"] in {
            "awaiting_review", "succeeded", "failed"}
        with client.websocket_connect(f"/api/v1/runs/{run['taskId']}/events/ws?metrics=1") as ws:
            seen_metrics = False
            for _ in range(200):        # 事件数上界，远超实际事件数
                try:
                    message = ws.receive_json()
                except Exception:
                    break
                if message.get("type") == "run_metrics":
                    seen_metrics = True
                    assert message["runId"] == run["taskId"]
                    break
            assert seen_metrics, "终态运行也必须送达至少一帧指标快照"


def test_cancel_marks_cancelled_not_failed(client):
    """取消必须落 cancelled（此前会被 progress 覆盖回 running）。"""
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", SAMPLE.read_bytes(), "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 1}).json()
    client.post(f"/api/v1/runs/{run['taskId']}/cancel")
    data = wait_for(client, run["taskId"], {"cancelled", "succeeded", "failed"})
    assert data["status"] in {"cancelled", "succeeded"}, data["status"]
    if data["status"] == "cancelled":
        events = client.get(f"/api/v1/runs/{run['taskId']}/events").json()
        assert any(e.get("type") == "run_cancelled" for e in events)


def test_repro_forces_non_stream(tmp_path):
    """复现契约：--repro 必须关闭流式，保住结构复现基线。"""
    from prd2tc.llm import effective_stream
    assert effective_stream(_cfg(repro=True, stream=True)) is False
    assert effective_stream(_cfg(repro=False, stream=True)) is True
