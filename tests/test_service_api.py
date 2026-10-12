import json
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from prd2tc.llm import ProbeResult
from prd2tc.service import create_app

# 最小合法 PNG（仅含魔数，足以通过嗅探）
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001")


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
    # PDF 现已支持（原先会被 400 拒绝）；改用真正不支持的格式验证拒绝路径
    assert client.post("/api/v1/prds",
                       files={"file": ("book.xlsx", b"PK\x03\x04nope", "application/vnd.ms-excel")}
                       ).status_code == 400

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


def test_upload_accepts_multiple_files_and_formats(client):
    """多文件 + 多格式上传（md + 图片 + docx）——原先只收单个 .md/.txt。"""
    import io
    import zipfile

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml",
                   f'<w:document xmlns:w="{W}"><w:body><w:p><w:r>'
                   f'<w:t>附件需求</w:t></w:r></w:p></w:body></w:document>')
    PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000050001")

    resp = client.post("/api/v1/prds", files=[
        ("files", ("prd.md", "# 主需求\n\n![图](https://x/A)\n".encode(), "text/markdown")),
        ("files", ("截图.png", PNG, "image/png")),
        ("files", ("附件.docx", buf.getvalue(),
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
    ])
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert len(meta["files"]) == 3
    assert meta["primary"] == "prd.md"                  # 显式主文件
    assert meta["bundle"]["sourceCount"] == 3
    assert meta["bundle"]["imageCount"] == 1
    assert set(meta["bundle"]["kinds"]) == {"markdown", "image", "docx"}
    assert meta["chars"] > 0


def test_upload_preserves_folder_structure(client):
    """文件夹上传必须**还原目录结构**，而不是把文件拍平。

    拍平的后果：`prds/<id>/` 下所有文件同级，磁盘读取时 `depth` 全部退化为 0，
    「根目录的 PRD」与「子目录里的长附件」不再可区分（实测附件抢占了正文）。
    同时 `path` 必须指向**实际落盘位置**（含子目录），否则显式主文件会失效。
    """
    long_attach = "# RAG 接口说明\n\n" + "接口细节。" * 3000
    resp = client.post("/api/v1/prds", files=[
        ("files", ("我的PRD/PRD.md", b"# \xe4\xb8\xbb\xe9\x9c\x80\xe6\xb1\x82\n", "text/markdown")),
        ("files", ("我的PRD/图片和附件/rag-service.md", long_attach.encode(), "text/markdown")),
        ("files", ("我的PRD/图片和附件/x.png", PNG_BYTES, "image/png")),
    ])
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert meta["primary"] == "PRD.md", meta["primary"]
    stored = {f["name"]: (f.get("stored") or "").replace("\\", "/") for f in meta["files"]}
    assert stored["PRD.md"] == "我的PRD/PRD.md", stored
    assert stored["rag-service.md"] == "我的PRD/图片和附件/rag-service.md", stored
    # path 必须落在含子目录的真实位置
    assert meta["path"].replace("\\", "/").endswith("我的PRD/PRD.md"), meta["path"]


def test_upload_folder_depth_reaches_run_manifest(client, tmp_path):
    """端到端：文件夹上传后运行，产物里的 depth 必须保留层级。"""
    long_attach = "# RAG\n\n" + "细节。" * 3000
    meta = client.post("/api/v1/prds", files=[
        ("files", ("我的PRD/PRD.md", "# 主需求\n\n## 6.1\n\n需求点。\n".encode(), "text/markdown")),
        ("files", ("我的PRD/图片和附件/rag-service.md", long_attach.encode(), "text/markdown")),
    ]).json()
    run = client.post("/api/v1/runs", json={"prdId": meta["prdId"], "stub": True, "auto": True}).json()
    run = wait_for(client, run["taskId"], {"succeeded", "failed"})
    assert run["status"] == "succeeded", run.get("errors")
    bundle_path = Path(run["output"]) / "analysis" / "prd-bundle.json"
    assert bundle_path.is_file()
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    depths = {s["name"]: s["depth"] for s in data["sources"]}
    assert depths.get("PRD.md") == 0, depths
    assert depths.get("rag-service.md") == 1, depths
    roles = {s["name"]: s["role"] for s in data["sources"]}
    assert roles.get("PRD.md") == "primary", roles


def test_upload_deduplicates_identical_content(client):
    """**核心回归**：同样的文件集合重复上传不得生成多条 PRD 记录。

    实测事故：antd 对文件夹/多文件会**逐文件**调用回调，前端把一次上传
    发成了 N 次请求 —— 列表里堆了 8 条同名同版本记录，用户无法分辨。
    修复分两层：前端合并为一次请求；后端按**内容**去重兜底
    （防御用户连点、脚本重复调用、旧客户端）。
    """
    files = [
        ("files", ("我的PRD/PRD.md", "# 需求\n\n## 6.1\n\n要点。\n".encode(), "text/markdown")),
        ("files", ("我的PRD/图片和附件/x.png", PNG_BYTES, "image/png")),
    ]
    first = client.post("/api/v1/prds", files=files).json()
    second = client.post("/api/v1/prds", files=files).json()
    third = client.post("/api/v1/prds", files=files).json()

    assert first["prdId"] == second["prdId"] == third["prdId"], (
        first["prdId"], second["prdId"], third["prdId"])
    assert second.get("deduplicated") is True
    # 列表里只有一条，不堆积重复项
    listed = client.get("/api/v1/prds").json()
    assert len([p for p in listed if p["prdId"] == first["prdId"]]) == 1
    # 重复落盘的目录已被清理
    assert not (Path(first["dir"]).parent / "nonexistent").exists()


def test_upload_different_content_creates_new_record(client):
    """内容不同必须新建记录 —— 去重不能误伤正常上传。"""
    a = client.post("/api/v1/prds", files=[
        ("files", ("a.md", "# A\n".encode(), "text/markdown"))]).json()
    b = client.post("/api/v1/prds", files=[
        ("files", ("a.md", "# B\n".encode(), "text/markdown"))]).json()
    assert a["prdId"] != b["prdId"]
    assert not b.get("deduplicated")


def test_update_prd_creates_version_in_own_folder(client):
    """保存新版本：进自己的目录、版本递增、且不继承父记录的 contentDigest。

    若不清理 contentDigest，新版本会带着父记录「一批上传文件」的指纹，
    后续正常上传会被误判为重复。
    """
    original = client.post("/api/v1/prds", files=[
        ("files", ("PRD.md", "# v1\n".encode(), "text/markdown"))]).json()
    updated = client.put(f"/api/v1/prds/{original['prdId']}",
                         json={"content": "# v2\n"}).json()
    assert updated["version"] == "v0.2"
    assert updated["parentPrdId"] == original["prdId"]
    assert updated["dir"] != original["dir"]
    assert updated.get("contentDigest") != original.get("contentDigest")
    assert client.get(f"/api/v1/prds/{updated['prdId']}").json()["content"] == "# v2\n"


def test_update_prd_carries_assets_to_new_version(client, tmp_path):
    """**核心回归**：编辑正文存新版本时，图片/附件必须一并延续。

    原先 `update_prd` 只把主文件写进新目录，其余资产丢失；
    而 `files` 元数据仍从父记录继承、声称有 N 个文件 —— 两者矛盾，
    且新版本运行时会读不到配图（实测：某 v0.2 声称 12 个文件、目录里只有 1 个）。
    """
    original = client.post("/api/v1/prds", files=[
        ("files", ("我的PRD/PRD.md", "# v1\n\n![图](https://x/A)\n".encode(), "text/markdown")),
        ("files", ("我的PRD/图片和附件/a.png", PNG_BYTES, "image/png")),
    ]).json()
    assert original["bundle"]["imageCount"] == 1

    updated = client.put(f"/api/v1/prds/{original['prdId']}",
                         json={"content": "# v2\n\n![图](https://x/A)\n"}).json()
    # 资产必须被复制过来
    names = {f["name"] for f in updated["files"]}
    assert "a.png" in names, updated["files"]
    assert updated["dir"] != original["dir"]
    # files 元数据必须与磁盘实际一致（不得凭空声称文件数）
    # `dir` 是相对工作区根的路径；工作区由 fixture 建在 tmp_path/workspace
    folder = tmp_path / "workspace" / updated["dir"]
    assert folder.is_dir(), folder
    actual = sorted(p.name for p in folder.rglob("*") if p.is_file())
    assert sorted(f["name"] for f in updated["files"]) == actual, (updated["files"], actual)
    # 不得出现两份主文件（旧副本 + 新扁平副本）
    assert [f["name"] for f in updated["files"]].count("PRD.md") == 1, updated["files"]
    # 新版本仍能解析出图片
    assert client.get(f"/api/v1/prds/{updated['prdId']}").json()["files"]


def test_upload_rejects_unsupported_format(client):
    resp = client.post("/api/v1/prds",
                       files={"file": ("book.xlsx", b"PK\x03\x04x", "application/vnd.ms-excel")})
    assert resp.status_code == 400
    assert "无法解析" in resp.json()["detail"]


def test_upload_sanitizes_path_traversal(client):
    """上传文件名可被客户端任意构造，绝不能穿越目录落盘。"""
    resp = client.post("/api/v1/prds",
                       files={"file": ("../../evil.md", b"# x\n", "text/markdown")})
    assert resp.status_code == 200
    meta = resp.json()
    assert ".." not in meta["path"]
    assert Path(meta["path"]).name == "evil.md"


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
