import json
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from prd2tc.llm import ProbeResult
from prd2tc.service import create_app


def _stub_probe(monkeypatch, *, ok=True, kind="ok", message="连接成功，端点可用", hint="",
                http_status=None, suggested_base="", model="m"):
    """替换真实探测：单元测试不得访问网络。"""
    monkeypatch.setattr("prd2tc.service.probe_provider",
                        lambda cfg, **kw: ProbeResult(ok=ok, kind=kind, message=message, hint=hint,
                                                      http_status=http_status, suggested_base=suggested_base,
                                                      model=model, latency_ms=1,
                                                      endpoint=(cfg.api_base or "") + "/chat/completions"))


def wait_for(client, run_id, wanted, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/v1/runs/{run_id}").json()
        if data.get("status") in wanted:
            return data
        time.sleep(0.05)
    return client.get(f"/api/v1/runs/{run_id}").json()


@pytest.fixture()
def client(tmp_path):
    app = create_app(state_dir=tmp_path / "workspace", max_workers=2)
    with TestClient(app) as value:
        yield value


def test_prd_versions_and_upload_validation(client):
    response = client.post("/api/v1/prds", files={"file": ("story.md", b"# Story\nhello", "text/markdown")})
    assert response.status_code == 200
    original = response.json()
    assert original["version"] == "v0.1"
    assert client.post("/api/v1/prds", files={"file": ("story.pdf", b"%PDF", "application/pdf")}).status_code == 400

    updated = client.put(f"/api/v1/prds/{original['prdId']}", json={"content": "# Story v2"})
    assert updated.status_code == 200
    newer = updated.json()
    assert newer["parentPrdId"] == original["prdId"]
    assert newer["version"] == "v0.2"
    assert client.get(f"/api/v1/prds/{original['prdId']}").json()["content"] == "# Story\nhello"


def test_stub_run_stage_approval_and_artifacts(client, tmp_path):
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(), "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 1}).json()
    assert run["mode"] == "stub"
    run = wait_for(client, run["taskId"], {"awaiting_review", "failed"})
    assert run["status"] == "awaiting_review"
    assert run["currentStage"] == "analysis"
    requirements = client.get(f"/api/v1/runs/{run['taskId']}/artifacts/analysis/requirements.json").json()["requirements"]
    assert client.put(f"/api/v1/runs/{run['taskId']}/stages/analysis", json={"requirements": requirements}).status_code == 200
    approved = client.post(f"/api/v1/runs/{run['taskId']}/stages/analysis/approve", json={}).json()
    assert approved["currentStage"] == "plan"
    approved = wait_for(client, run["taskId"], {"awaiting_review", "failed"})
    assert approved["currentStage"] == "plan"
    approved = client.post(f"/api/v1/runs/{run['taskId']}/stages/plan/approve", json={}).json()
    assert approved["currentStage"] == "cases"
    approved = wait_for(client, run["taskId"], {"awaiting_review", "failed"})
    assert approved["currentStage"] == "cases"
    final = client.post(f"/api/v1/runs/{run['taskId']}/stages/cases/approve", json={}).json()
    assert final["status"] == "succeeded"
    files = client.get(f"/api/v1/runs/{run['taskId']}/artifacts").json()
    paths = {item["path"] for item in files}
    assert "analysis/requirements.json" in paths
    assert "cases/traceability.json" in paths


def test_one_click_auto_run_runs_all_stages_without_approvals(client, tmp_path):
    """一键运行（auto）应一次跑完 分析→方案→用例 并 succeeded，无需逐阶段批准。"""
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(), "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "auto": True, "stub": True, "jobs": 1}).json()
    assert run["auto"] is True
    run = wait_for(client, run["taskId"], {"succeeded", "failed"})
    assert run["status"] == "succeeded", run.get("errors")
    assert run["currentStage"] == "done"
    for stage in ("analysis", "plan", "cases"):
        assert run["stageStates"].get(stage) == "succeeded"
        assert run["review"][stage]["status"] == "auto"
    files = client.get(f"/api/v1/runs/{run['taskId']}/artifacts").json()
    paths = {item["path"] for item in files}
    assert "analysis/requirements.json" in paths and "cases/traceability.json" in paths


def test_llm_key_is_not_persisted(client, tmp_path, monkeypatch):
    _stub_probe(monkeypatch)  # 不打击真实网络
    response = client.post("/api/v1/sessions/llm-config", json={"provider": "openai", "model": "x", "apiKey": "secret-key"})
    assert response.status_code == 200
    assert "apiKey" not in response.text
    assert response.json()["probe"]["ok"] is True
    state = tmp_path / "workspace"
    assert "secret-key" not in "\n".join(p.read_text(encoding="utf-8") for p in state.rglob("*") if p.is_file())


def test_llm_config_rejected_when_probe_fails(client, monkeypatch):
    """保存前检测不通过必须拒绝，并回传结构化原因（本次真实故障：官网地址 404）。"""
    _stub_probe(monkeypatch, ok=False, kind="endpoint", http_status=404,
                message="HTTP 404 且返回网页而非 API 响应（MiniMax API 开放平台）",
                hint="apiBase 很可能填成了控制台/官网地址", suggested_base="https://platform.minimax.cn/v1")
    response = client.post("/api/v1/sessions/llm-config",
                           json={"provider": "openai", "model": "minimax",
                                 "apiBase": "https://platform.minimax.cn", "apiKey": "k"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["kind"] == "endpoint" and detail["http_status"] == 404
    assert "网页" in detail["message"]
    assert detail["suggested_base"] == "https://platform.minimax.cn/v1"
    assert "拒绝保存" in detail["notice"]


def test_llm_verify_probes_without_creating_session(client, monkeypatch):
    _stub_probe(monkeypatch, ok=False, kind="auth", http_status=401, message="HTTP 401 鉴权失败")
    body = {"provider": "openai", "model": "m", "apiBase": "https://api.example.com/v1", "apiKey": "bad"}
    assert client.post("/api/v1/sessions/llm-config/verify", json=body).json()["kind"] == "auth"
    # verify 不落任何会话；因此用它拿到的 sessionId 去建 run 应当被拒
    run = client.post("/api/v1/runs", json={"prdId": "nope", "sessionId": "not-a-session"})
    assert run.status_code == 400


def test_legacy_run_rejects_invalid_stage(client):
    """非法 stage 必须 400，而不是创建出永久 pending 的任务。"""
    prd = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    response = client.post("/runs", json={"prd": str(prd), "stage": "bogus"})
    assert response.status_code == 400
    assert "stage" in response.json()["detail"]


def test_export_endpoints(client):
    """导出接口：Excel / XMind 下载，非法格式与未知运行分别 400 / 404。"""
    import zipfile
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                       "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "auto": True}).json()
    run = wait_for(client, run["taskId"], {"succeeded", "failed"})
    assert run["status"] == "succeeded", run.get("errors")

    # 自动导出：产物清单里应出现两种格式
    paths = {item["path"] for item in client.get(f"/api/v1/runs/{run['taskId']}/artifacts").json()}
    assert any(p.endswith(".xlsx") for p in paths), paths
    assert any(p.endswith(".xmind") for p in paths), paths

    for fmt, ctype in (("excel", "spreadsheetml.sheet"), ("xmind", "vnd.xmind.workbook")):
        resp = client.get(f"/api/v1/exports/{run['taskId']}/{fmt}")
        assert resp.status_code == 200, fmt
        assert ctype in resp.headers["content-type"], resp.headers["content-type"]
        assert "attachment" in resp.headers.get("content-disposition", "")
        body = resp.content
        assert body[:4] == b"PK\x03\x04"
        with zipfile.ZipFile(__import__("io").BytesIO(body)) as z:
            assert z.testzip() is None

    assert client.get(f"/api/v1/exports/{run['taskId']}/pdf").status_code == 400
    assert client.get("/api/v1/exports/no-such-run/excel").status_code == 404


def test_binary_artifact_download_does_not_crash(client):
    """二进制产物（xlsx/xmind）不得走 read_text —— 否则 UnicodeDecodeError。

    这是引入导出功能时暴露的连带问题：artifact 接口原先假定产物都是文本。
    """
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                       "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "auto": True}).json()
    run = wait_for(client, run["taskId"], {"succeeded", "failed"})
    assert run["status"] == "succeeded"

    binary = [item["path"] for item in
              client.get(f"/api/v1/runs/{run['taskId']}/artifacts").json()
              if item["path"].endswith((".xlsx", ".xmind"))]
    assert binary, "应存在二进制产物"
    for path in binary:
        resp = client.get(f"/api/v1/runs/{run['taskId']}/artifacts/{path}")
        assert resp.status_code == 200, (path, resp.status_code)
        assert resp.content[:4] == b"PK\x03\x04", path


def test_websocket_event_endpoint_accepts_connections(tmp_path):
    app = create_app(state_dir=tmp_path / "workspace")
    with TestClient(app) as value:
        with value.websocket_connect("/api/v1/runs/run-unknown/events/ws"):
            pass


def test_production_spa_fallback(tmp_path):
    ui_dist = Path(__file__).parents[1] / "ui" / "dist"
    if not (ui_dist / "index.html").exists():
        pytest.skip("前端尚未构建")
    with TestClient(create_app(state_dir=tmp_path / "workspace", ui_dir=ui_dist)) as value:
        assert value.get("/").status_code == 200
        assert value.get("/runs/new").status_code == 200
        assert "id=\"root\"" in value.get("/").text
