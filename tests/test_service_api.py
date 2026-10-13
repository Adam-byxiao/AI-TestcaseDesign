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


def wait_for(client, run_id, wanted, timeout=20):
    """轮询直到运行进入目标状态。

    超时从 8s 放宽到 20s：stub 三阶段要跑线程池、写多个产物并做文件锁，
    在整文件并发执行（多个 TestClient/线程池同时活动）时可能超过 8s ——
    实测出现过「单跑通过、整文件失败」的假失败。放宽只为消除该噪声，
    真实卡死仍会被 pytest 的用例超时兜住。
    """
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


def test_request_roundtrip_preserves_path_list(client):
    """**核心回归**：多文件请求经 task.json 往返后，`prd` 必须仍是路径列表。

    实测事故：`request_dict` 写成 `str(req.prd)`，多文件时存成
    `"[WindowsPath('D:/a.md'), WindowsPath('D:/b.png')]"`。
    阶段一用内存里的原始请求故能成功；阶段二从 task.json 重建时
    这串文本被当作单个路径 → `ValueError: 没有可用的输入（输入不存在: [...]）`。
    """
    from prd2tc.service import _decode_prd, _encode_prd
    paths = [Path("D:/w/prd.md"), Path("D:/w/图/x.png")]
    encoded = _encode_prd(paths)
    assert isinstance(encoded, list) and all(isinstance(x, str) for x in encoded), encoded
    # JSON 往返（task.json 的真实存储方式）
    decoded = _decode_prd(json.loads(json.dumps(encoded)))
    assert decoded == paths, decoded
    # 单文件仍为标量
    assert _encode_prd(Path("D:/w/a.md")) == str(Path("D:/w/a.md"))
    assert _decode_prd(_encode_prd(Path("D:/w/a.md"))) == Path("D:/w/a.md")


def test_legacy_stringified_prd_list_is_recovered():
    """旧的字符串化列表必须能还原 —— 否则已失败的任务无法恢复、用户要重传。"""
    from prd2tc.service import _decode_prd
    legacy = "[WindowsPath('D:/a/b.md'), WindowsPath('D:/a/c.png')]"
    assert _decode_prd(legacy) == [Path("D:/a/b.md"), Path("D:/a/c.png")]
    # 普通字符串（正常单文件）不得被误判为列表
    assert _decode_prd("D:/a/b.md") == Path("D:/a/b.md")


def test_build_request_passes_all_files_and_survives_stage_handoff(client, tmp_path):
    """上传多文件后：请求携带全部文件，且经 request_dict/request_from 往返不丢信息。"""
    from prd2tc.service import _decode_prd
    meta = client.post("/api/v1/prds", files=[
        ("files", ("我的PRD/PRD.md", "# 需求\n".encode(), "text/markdown")),
        ("files", ("我的PRD/图片和附件/x.png", PNG_BYTES, "image/png")),
    ]).json()
    body = {"product": "p", "version": "v1"}
    task = client.post("/api/v1/runs", json={**body, "prdId": meta["prdId"],
                                             "stub": True, "auto": True}).json()
    # 该运行能跑到成功（阶段一与后续阶段共用同一序列化路径）
    run = wait_for(client, task["taskId"], {"succeeded", "failed"})
    assert run["status"] == "succeeded", run.get("errors")
    # 请求里的 prd 落盘后必须是列表（不是字符串化文本）
    workspace = tmp_path / "workspace" / "tasks" / f"{task['taskId']}.json"
    payload = json.loads(workspace.read_text(encoding="utf-8"))
    stored = payload["request"]["prd"]
    assert isinstance(stored, list), stored
    assert len(stored) >= 2, stored
    assert all("WindowsPath(" not in str(x) for x in stored), stored


def _make_session(client, monkeypatch, *, api_key="sk-test", provider="openai"):
    """写入一个 LLM 会话并返回 sessionId。

    走真实端点（验证其行为），但**打桩探测** —— 该端点会真实探测网络并拒绝保存，
    单元测试不得访问网络。
    """
    _stub_probe(monkeypatch)
    resp = client.post("/api/v1/sessions/llm-config", json={
        "provider": provider, "apiBase": "https://api.example.com/v1",
        "model": "m", "apiKey": api_key})
    assert resp.status_code == 200, resp.text
    return resp.json()["sessionId"]


def test_approved_stage_uses_freshly_provided_session(client, monkeypatch):
    """**核心回归**：阶段推进必须采用**本次请求带来的**新会话。

    实测事故：用户重新在「模型配置」保存后，点「批准」仍报「未配置 LLM 凭证」。
    根因是端点用 task.json 里记录的**旧** sessionId —— 服务端会话是内存态、
    TTL 1 小时且随重启失效；用户新配的会话压根没被用上。
    """
    # 旧会话写入任务后失效（模拟服务重启/TTL 过期）
    stale = _make_session(client, monkeypatch, api_key="sk-stale")
    fresh = _make_session(client, monkeypatch, api_key="sk-fresh")
    assert stale != fresh

    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True,
                                            "jobs": 1, "sessionId": stale}).json()
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    assert run["currentStage"] == "analysis", run

    # 让旧会话失效：直接清掉（等价于服务重启 / TTL 过期）
    store = _store_of(client)
    store.sessions.pop(stale, None)

    # 用**新会话**批准：必须成功，且新会话被写回任务
    resp = client.post(f"/api/v1/runs/{run['taskId']}/stages/analysis/approve",
                       json={"sessionId": fresh})
    assert resp.status_code == 200, resp.text
    after = client.get(f"/api/v1/runs/{run['taskId']}").json()
    assert after.get("sessionId") == fresh, after.get("sessionId")


def test_approve_with_expired_session_reports_expiry_not_missing_credentials(client, monkeypatch):
    """会话失效时必须报「会话已过期」，而不是误导性的「未配置 LLM 凭证」。

    用户按「未配置」的提示反复重新配置，却始终失败 —— 实测最令人困惑之处。
    """
    stale = _make_session(client, monkeypatch)
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True,
                                            "sessionId": stale}).json()
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    _store_of(client).sessions.pop(stale, None)

    # 明确带来一个失效的 sessionId → 立刻 401 且说明是过期
    resp = client.post(f"/api/v1/runs/{run['taskId']}/stages/analysis/approve",
                       json={"sessionId": stale})
    assert resp.status_code == 401, resp.text
    assert "过期" in resp.json()["detail"], resp.json()


def test_approve_does_not_pollute_draft_with_session_id(client, monkeypatch):
    """`sessionId` 是控制参数，绝不能混进草稿体。

    `save_draft` 会对 body 整体做哈希并原样存为草稿内容 —— 混进去会污染草稿
    与 contentHash（进而影响「已审阅内容」的指纹比对）。
    """
    session = _make_session(client, monkeypatch)
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True,
                                            "sessionId": session}).json()
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    resp = client.post(f"/api/v1/runs/{run['taskId']}/stages/analysis/approve",
                       json={"sessionId": session, "markdown": "# 我改过的分析"})
    assert resp.status_code == 200, resp.text
    drafts = list((_workspace_of(client) / "drafts" / run["taskId"]).glob("analysis.json"))
    assert drafts, "草稿未落盘"
    saved = json.loads(drafts[0].read_text(encoding="utf-8"))
    assert "sessionId" not in json.dumps(saved.get("content") or {}, ensure_ascii=False)
    assert saved["content"].get("markdown") == "# 我改过的分析"


def test_resume_and_retry_accept_fresh_session(client, monkeypatch):
    """恢复与模块重试同样必须支持传入新会话（否则同一类困惑会复现）。"""
    stale = _make_session(client, monkeypatch)
    fresh = _make_session(client, monkeypatch)
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True,
                                            "sessionId": stale}).json()
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    _store_of(client).sessions.pop(stale, None)

    # 恢复：带新会话应被接受并写回
    resp = client.post(f"/api/v1/runs/{run['taskId']}/resume", json={"sessionId": fresh})
    assert resp.status_code in {200, 409}, resp.text        # 409 = 当前状态不可恢复，非本测试关注点
    if resp.status_code == 200:
        assert client.get(f"/api/v1/runs/{run['taskId']}").json().get("sessionId") == fresh

    # 过期会话：明确 401，而不是静默降级
    resp = client.post(f"/api/v1/runs/{run['taskId']}/resume", json={"sessionId": stale})
    assert resp.status_code == 401, resp.text
    assert "过期" in resp.json()["detail"], resp.json()


def test_export_filename_is_sanitized(client, tmp_path):
    """**核心回归**：下载名来自查询参数，必须清洗后才能进 Content-Disposition。

    未清洗时 `"` 或换行可造成**响应头注入**，`../` 可能被客户端当作路径。
    """
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 1}).json()
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    client.post(f"/api/v1/runs/{run['taskId']}/stages/analysis/approve", json={})
    wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    client.post(f"/api/v1/runs/{run['taskId']}/stages/plan/approve", json={})
    wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})

    resp = client.get(f"/api/v1/exports/{run['taskId']}/excel",
                      params={"filename": "我的用例集"})
    assert resp.status_code == 200, resp.text
    disposition = resp.headers["content-disposition"]
    assert "我的用例集.xlsx" in disposition or "%E6%88%91" in disposition, disposition

    # 路径穿越/引号/换行都必须被清洗
    bad = client.get(f"/api/v1/exports/{run['taskId']}/excel",
                     params={"filename": '../../etc/pa"ss\nwd'})
    assert bad.status_code == 200
    header = bad.headers["content-disposition"]
    assert "\n" not in header and header.count('"') <= 2, header
    assert "passwd.xlsx" in header or "pa_ss_wd.xlsx" in header, header


def test_export_rejects_unknown_options(client):
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 1}).json()
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    assert client.get(f"/api/v1/exports/{run['taskId']}/pdf").status_code == 400
    assert client.get(f"/api/v1/exports/{run['taskId']}/xmind",
                      params={"xmind_mode": "bogus"}).status_code == 400


def test_run_exposes_stage_meta(client):
    """三个阶段要**分开呈现**，因此运行详情需暴露逐阶段元数据（耗时/模型/token）。"""
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 1}).json()
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    detail = client.get(f"/api/v1/runs/{run['taskId']}").json()
    assert "stageMeta" in detail, detail.keys()
    assert "analysis" in (detail["stageMeta"] or {}), detail.get("stageMeta")


def test_regenerate_stage_reruns_only_that_stage(client, tmp_path):
    """**核心价值**：单独重跑某阶段，保留其它阶段产物（不必整轮重来）。"""
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 1}).json()
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    root = Path(client.get(f"/api/v1/runs/{run['taskId']}").json()["output"])
    plan = root / "plan" / "测试方案.md"

    # 先把三个阶段都跑完，得到完整产物
    client.post(f"/api/v1/runs/{run['taskId']}/stages/analysis/approve", json={})
    wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    client.post(f"/api/v1/runs/{run['taskId']}/stages/plan/approve", json={})
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    assert run.get("currentStage") == "cases", run
    assert plan.is_file()
    plan_mtime = plan.stat().st_mtime
    analysis_before = (root / "analysis" / "requirements.json").read_text(encoding="utf-8")

    # 重跑阶段 3：只应丢弃用例产物
    resp = client.post(f"/api/v1/runs/{run['taskId']}/stages/cases/regenerate", json={})
    assert resp.status_code == 200, resp.text
    assert plan.stat().st_mtime == plan_mtime, "阶段 2 产物不应被动"
    assert (root / "analysis" / "requirements.json").read_text(encoding="utf-8") == analysis_before
    after = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    assert after["status"] != "failed", after.get("errors")


def test_regenerate_analysis_discards_downstream(client):
    """重跑上游阶段必须**一并丢弃下游**产物 —— 否则下游与新上游不自洽。"""
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 1}).json()
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    root = Path(client.get(f"/api/v1/runs/{run['taskId']}").json()["output"])
    client.post(f"/api/v1/runs/{run['taskId']}/stages/analysis/approve", json={})
    wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    client.post(f"/api/v1/runs/{run['taskId']}/stages/plan/approve", json={})
    wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    assert (root / "plan" / "测试方案.md").is_file()

    resp = client.post(f"/api/v1/runs/{run['taskId']}/stages/analysis/regenerate", json={})
    assert resp.status_code == 200, resp.text
    discarded = resp.json().get("discarded") or []
    assert any("plan" in d for d in discarded), discarded
    assert not (root / "plan" / "测试方案.md").exists(), "下游产物应被丢弃"


def test_regenerate_rejects_bad_stage_and_running_run(client):
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True, "jobs": 1}).json()
    assert client.post(f"/api/v1/runs/{run['taskId']}/stages/nope/regenerate",
                       json={}).status_code == 400
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    # 等待审阅时不算「进行中」，可以重跑；构造一个进行中的状态来验证 409
    _store_of(client).save_task(run["taskId"], status="running")
    assert client.post(f"/api/v1/runs/{run['taskId']}/stages/cases/regenerate",
                       json={}).status_code == 409


def _workspace_of(client) -> Path:
    return _store_of(client).root


def _store_of(client):
    """取出 service 的 WorkspaceStore（create_app 挂在 app.state.store 上）。"""
    store = getattr(client.app.state, "store", None)
    assert store is not None and hasattr(store, "sessions"), "未找到 WorkspaceStore"
    return store


def test_credential_error_distinguishes_expiry_from_missing_config():
    """凭证提示策略必须区分「会话过期」与「从未配置」。

    为什么单独测这个纯函数：stub 模式下 `run_task` 不会走到该分支，
    这两条消息**无法**通过端点测试触达 —— 变异测试曾因此漏报
    （把「会话过期」的提示删掉，所有测试仍然全绿）。
    """
    from prd2tc.service import _credential_error

    base = dict(stub=False, dry_run=False, requires_key=True)

    # 有可用 key：无问题
    assert _credential_error(**base, has_key=True, session_id="s", session_alive=True) is None
    # stub / dry-run 不校验凭证
    assert _credential_error(stub=True, dry_run=False, requires_key=True, has_key=False,
                             session_id="s", session_alive=False) is None
    assert _credential_error(stub=False, dry_run=True, requires_key=True, has_key=False,
                             session_id="s", session_alive=False) is None
    # ollama 之类不需要 key
    assert _credential_error(stub=False, dry_run=False, requires_key=False, has_key=False,
                             session_id=None, session_alive=False) is None

    # 带了会话但已失效 → 必须说「过期」（可操作指引）
    expired = _credential_error(**base, has_key=False, session_id="s", session_alive=False)
    assert expired and "过期" in expired, expired
    assert "恢复" in expired, expired           # 要给出下一步动作

    # 压根没会话也没 key → 说「未配置」
    missing = _credential_error(**base, has_key=False, session_id=None, session_alive=False)
    assert missing and "未配置" in missing, missing
    assert "过期" not in missing, missing       # 不得混用两种原因


def test_run_creation_rejects_expired_session_before_queueing(client, monkeypatch):
    """创建运行时若带来已失效的 sessionId，必须**立刻 401**、不入队、不产生任务。

    比「入队后失败」更好：用户立即得到明确原因，也不会留下一条失败记录。
    """
    stale = _make_session(client, monkeypatch)
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    _store_of(client).sessions.pop(stale, None)

    # 非 stub：会真正校验凭证（stub 会跳过整段检查）
    resp = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": False,
                                             "sessionId": stale, "auto": True})
    assert resp.status_code == 401, resp.text
    assert "过期" in resp.json()["detail"], resp.json()
    # 没有产生失败任务
    tasks = [t for t in client.get("/api/v1/runs").json() if t.get("prdId") == prd["prdId"]]
    assert tasks == [], tasks


def test_legacy_run_endpoint_reports_credential_problem(client, monkeypatch):
    """**接线回归**：旧版 `POST /runs` 走 `run_task` 且不传 sessionId —— 它正是
    能到达 `run_task` 凭证检查的路径（新版端点会在创建时就拦截）。

    该端点原先**完全没有凭证校验**，会一路走到 pipeline 才报错，
    错误信息也含糊（用户看不出该去配模型还是该恢复运行）。
    这里断言：任务失败且原因明确指向「未配置凭证 / 会话过期」其中之一。
    """
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    # 非 stub 且不提供会话 → 必然缺少凭证
    resp = client.post("/runs", json={"prd": str(sample), "stub": False})
    assert resp.status_code == 202, resp.text
    run = wait_for(client, resp.json()["taskId"], {"failed", "succeeded"})
    assert run["status"] == "failed", run
    blob = json.dumps(run.get("errors") or [], ensure_ascii=False)
    assert ("未配置 LLM 凭证" in blob) or ("过期" in blob), blob


def test_approve_without_session_after_expiry_reports_expiry(client, monkeypatch):
    """**可观测路径**：任务里存有会话、会话随后失效、用户**不带** sessionId 直接批准。

    这是真实场景（服务重启后用户直接点「批准」，没意识到需要重配）。
    此时 `_resolve_session` 回退到任务里的旧 ID（取不到会话但不报错），
    必须由 `run_task` 的凭证前置校验报出「过期」。

    观测方式：凭证检查发生在**构造 PipelineRunner 之前**，
    因此打桩 Runner 并断言它**从未被构造** —— 这是与「删掉前置校验」的
    确定性区别（后者会一路走到 Runner 才失败，且消息含糊）。
    """
    import prd2tc.service as _svc

    session = _make_session(client, monkeypatch)
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    prd = client.post("/api/v1/prds", files={"file": ("sample-prd.md", sample.read_bytes(),
                                                      "text/markdown")}).json()
    run = client.post("/api/v1/runs", json={"prdId": prd["prdId"], "stub": True,
                                            "sessionId": session}).json()
    run = wait_for(client, run["taskId"], {"awaiting_review", "succeeded", "failed"})
    assert run["currentStage"] == "analysis", run
    _store_of(client).sessions.pop(session, None)     # 模拟重启/超时

    # 把**存下来的请求**改成非 stub（模拟真实模型运行）：
    # 用 stub 跑到待审阅可避免联网，但 stub 会正确地跳过凭证校验，
    # 因此必须翻转该标记才能真正观测到前置校验。
    stored = client.get(f"/api/v1/runs/{run['taskId']}").json()
    _store_of(client).save_task(run["taskId"],
                                request={**(stored.get("request") or {}), "stub": False})

    constructed: list = []

    class _FakeRunner:
        def __init__(self, *_a, **_k):
            constructed.append(True)
            self.live = type("L", (), {"snapshot": lambda self, force=False: {},
                                       "final_snapshot": lambda self: None,
                                       "set_stage": lambda self, s: None,
                                       "begin_module": lambda self, *a, **k: None,
                                       "finish_module": lambda self, *a, **k: None,
                                       "mark_cancelled": lambda self: None})()
            self.cancel_flag = type("C", (), {"is_set": lambda self: False})()

        def generate(self, *_a, **_k):
            raise AssertionError("前置凭证校验未生效：Runner 不该被构造")

        def cancel(self):
            pass

    monkeypatch.setattr(_svc, "PipelineRunner", _FakeRunner)

    # 不带 sessionId 批准 → 必须在前置校验处失败，Runner 不构造
    resp = client.post(f"/api/v1/runs/{run['taskId']}/stages/analysis/approve", json={})
    assert resp.status_code == 200, resp.text
    after = wait_for(client, run["taskId"], {"failed", "awaiting_review", "succeeded"})
    blob = json.dumps(after.get("errors") or [], ensure_ascii=False)
    assert "过期" in blob, blob
    assert "未配置 LLM 凭证" not in blob, blob
    assert constructed == [], "凭证校验应先于 Runner 构造"


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
