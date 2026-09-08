import json
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from prd2tc.service import create_app


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


def test_llm_key_is_not_persisted(client, tmp_path):
    response = client.post("/api/v1/sessions/llm-config", json={"provider": "openai", "model": "x", "apiKey": "secret-key"})
    assert response.status_code == 200
    assert "apiKey" not in response.text
    state = tmp_path / "workspace"
    assert "secret-key" not in "\n".join(p.read_text(encoding="utf-8") for p in state.rglob("*") if p.is_file())


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
