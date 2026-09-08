"""可选 FastAPI 适配层：文件工作区、阶段审阅和实时事件。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib, json, mimetypes, re, secrets, threading, time, uuid
from pathlib import Path
from typing import Any

from prd2tc import __version__, stages
from prd2tc.config import Config
from prd2tc.io import (atomic_write_text, now_iso, save_cases, save_requirements,
                       save_traceability, write_stage, render_cases_md)
from prd2tc.pipeline import GenerateRequest, PipelineRunner
from prd2tc.traceability import build_traceability, validate_traceability
from prd2tc.validator import validate_case

ALLOWED_PRD = {".md", ".markdown", ".txt"}
STAGES = ("analysis", "plan", "cases")

class TaskStore:
    """旧 `/runs` API 的兼容状态存储。"""
    def __init__(self, root: str | Path):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True); self._lock = threading.Lock()
        for path in self.root.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") in {"pending", "running"}:
                data.update(status="failed", stage="recovered", error="service restarted before task completion", updatedAt=now_iso())
                atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    def save(self, task_id: str, **values):
        path = self.root / f"{task_id}.json"
        with self._lock:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"taskId": task_id, "createdAt": now_iso()}
            data.update(values); data["updatedAt"] = now_iso(); atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"); return data
    def get(self, task_id: str):
        path = self.root / f"{task_id}.json"; return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

class WorkspaceStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve(); self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock(); self.sessions: dict[str, tuple[float, dict]] = {}
        self.tasks = self.root / "tasks"; self.prds = self.root / "prds"; self.events = self.root / "events"
        for d in (self.tasks, self.prds, self.events): d.mkdir(exist_ok=True)
        for path in self.tasks.glob("*.json"):
            data = self._json(path)
            if data.get("status") in {"pending", "running"}:
                data.update(status="failed", failedStage=data.get("currentStage"),
                            error="service restarted before task completion", updatedAt=now_iso())
                self.save_json(path, data)

    def _safe(self, path: Path) -> Path:
        path = path.resolve()
        if path != self.root and self.root not in path.parents: raise ValueError("路径超出工作区")
        return path
    def _json(self, path: Path, default=None):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else (default if default is not None else {})
    def save_json(self, path: Path, data: dict):
        atomic_write_text(self._safe(path), json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    def save_task(self, task_id: str, **values):
        path = self.tasks / f"{task_id}.json"
        with self._lock:
            data = self._json(path, {"taskId": task_id, "createdAt": now_iso()}); data.update(values); data["updatedAt"] = now_iso(); self.save_json(path, data); return data
    def task(self, task_id: str): return self._json(self.tasks / f"{task_id}.json", None)
    def list_tasks(self): return sorted((self._json(p) for p in self.tasks.glob("*.json")), key=lambda x: x.get("updatedAt", ""), reverse=True)
    def event(self, run_id: str, **values):
        event = {"eventId": f"evt-{uuid.uuid4().hex}", "runId": run_id, "createdAt": now_iso(), **values}
        path = self.events / f"{run_id}.jsonl"; path.parent.mkdir(exist_ok=True)
        with self._lock:
            previous = path.read_text(encoding="utf-8") if path.exists() else ""
            atomic_write_text(path, previous + json.dumps(event, ensure_ascii=False) + "\n")
        self.save_task(run_id, lastEventId=event["eventId"]); return event
    def event_list(self, run_id):
        path = self.events / f"{run_id}.jsonl"
        if not path.exists(): return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    def put_session(self, values: dict, ttl: int = 3600):
        sid = secrets.token_urlsafe(24); self.sessions[sid] = (time.time() + ttl, dict(values)); return sid
    def get_session(self, sid: str):
        item = self.sessions.get(sid)
        if not item or item[0] < time.time(): self.sessions.pop(sid, None); return None
        return dict(item[1])

    def redact(self, value: object) -> str:
        text = str(value)
        for _, session in self.sessions.values():
            key = str(session.get("apiKey", ""))
            if key:
                text = text.replace(key, "[REDACTED]")
        return text

def _content_hash(text: str) -> str: return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _next_version(value: str) -> str:
    match = re.fullmatch(r"v(\d+)\.(\d+)", value or "")
    return f"v{match.group(1)}.{int(match.group(2)) + 1}" if match else "v0.2"

def _safe_component(value: object, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-")
    return cleaned[:80] or fallback

def _safe_address(value: str) -> str:
    value = re.sub(r"(?<=://)[^/@]+@", "***@", value or "")
    return value.split("?", 1)[0]

def create_app(config: Config | None = None, *, state_dir: str | Path = ".prd2tc-workspace",
               max_workers: int = 2, ui_dir: str | Path | None = None):
    try:
        from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket
        from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
    except ImportError as exc: raise RuntimeError("服务模式需要安装: pip install 'prd2tc[service]'") from exc
    app = FastAPI(title="prd2tc service", version=__version__); store = WorkspaceStore(state_dir)
    executor = ThreadPoolExecutor(max_workers=max(1, max_workers)); base_config = config or Config.from_env()
    root = store.root

    def require_id(value: str, kind: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9-]+", value): raise HTTPException(400, f"非法 {kind}")
        return value

    def build_request(meta: dict, body: dict, task_id: str, *, stage: str = "1") -> GenerateRequest:
        product = _safe_component(body.get("product") or Path(meta["filename"]).stem, "product")
        version = _safe_component(body.get("version") or meta.get("version"), "v0.1")
        return GenerateRequest(
            prd=root / meta["path"], out=root / "outputs" / task_id, product=product,
            version=version, stub=bool(body.get("stub", False)), jobs=int(body.get("jobs", 1)),
            profile=body.get("profile", "generic"), stage=stage,
            max_gate_retries=int(body.get("maxGateRetries", 2)),
            allow_gate_fail=bool(body.get("allowGateFail", False)), no_gate=bool(body.get("noGate", False)))

    def stage_name(value: str): return {"1": "analysis", "2": "plan", "3": "cases", "all": "done"}[value]
    def request_dict(req: GenerateRequest):
        return {"prd": str(req.prd), "out": str(req.out), "product": req.product, "version": req.version,
                "stub": req.stub, "jobs": req.jobs, "profile": req.profile,
                "max_gate_retries": req.max_gate_retries, "allow_gate_fail": req.allow_gate_fail,
                "no_gate": req.no_gate}
    def request_from(data: dict, stage: str, resume=True):
        return GenerateRequest(**data, stage=stage, resume=resume)

    def run_task(task_id: str, req: GenerateRequest, session_id: str | None = None, review_mode: bool = True):
        session = store.get_session(session_id) if session_id else None
        runner_config = base_config
        if session:
            runner_config = replace(base_config, provider=session["provider"], model=session["model"], api_base=session["apiBase"], api_key=session.get("apiKey", ""), temperature=float(session.get("temperature", base_config.temperature)), timeout=int(session.get("timeout", base_config.timeout)))
        try:
            current = stage_name(req.stage); task = store.task(task_id) or {}; stage_states = dict(task.get("stageStates", {})); stage_states[current] = "running"
            store.save_task(task_id, status="running", currentStage=current, failedStage=None, stageStates=stage_states)
            store.event(task_id, stage=current, type="run_started", status="running", percent=task.get("percent", 0), message=f"开始 {current}")
            def progress(stage, percent):
                store.save_task(task_id, status="running", currentStage=stage, percent=percent)
                store.event(task_id, stage=stage, type="stage_progress", status="running", percent=percent, message=f"{stage} 进度 {percent}%")
            result = PipelineRunner(runner_config).generate(req, progress_callback=progress)
            pipeline_state = store._json(result.root / "run-state.json", {})
            modules = pipeline_state.get("modules", {})
            manifest = store._json(result.root / "run-manifest.json", {})
            gates = {name: (value.get("gate") or {}) for name, value in manifest.get("stages", {}).items()}
            stages_meta = manifest.get("stages") or {}
            tokens = {"total": sum(v.get("tokens", 0) for v in stages_meta.values()),
                      "stages": {name: {"tokens": v.get("tokens", 0), "moduleTokens": v.get("moduleTokens", {})}
                                 for name, v in stages_meta.items()}}
            if (store.task(task_id) or {}).get("status") == "cancelled":
                store.event(task_id, stage=current, type="run_cancelled", status="cancelled", percent=0, message="任务已取消"); return
            if result.status != "succeeded":
                # auto（stage=all）时把失败定位到具体子阶段（manifest 中 status=failed 的阶段）
                failed = next((s for s, v in stages_meta.items() if v.get("status") == "failed"), current)
                stage_states[failed] = "failed"
                store.save_task(task_id, status="failed", currentStage=failed, failedStage=failed, stageStates=stage_states, modules=modules, qualityGate=gates, tokens=tokens, errors=result.errors, output=str(result.root))
                store.event(task_id, stage=failed, type="run_failed", status="failed", percent=task.get("percent", 0), message=f"{failed} 门禁失败"); return
            stage_states[current] = "awaiting_review" if review_mode and current != "done" else "succeeded"
            status_value = "awaiting_review" if review_mode and current != "done" else "succeeded"
            # auto（stage=all）一键跑全流程：补写各子阶段状态与审阅记录，保证可审计
            review = dict(task.get("review", {}))
            if current == "done" and status_value == "succeeded":
                for sub in ("analysis", "plan", "cases"):
                    stage_states[sub] = "succeeded"
                    review.setdefault(sub, {"status": "auto", "approvedBy": "one-click", "approvedAt": now_iso()})
            store.save_task(task_id, status=status_value, currentStage=current if status_value == "awaiting_review" else "done", percent={"analysis":33,"plan":66,"cases":100,"done":100}[current], runId=result.run_id, output=str(result.root), errors=result.errors, stageStates=stage_states, review=review, modules=modules, qualityGate=gates, tokens=tokens)
            store.event(task_id, stage=current, type="stage_ready" if status_value == "awaiting_review" else "run_finished", status=status_value, percent={"analysis":33,"plan":66,"cases":100,"done":100}[current], message="等待用户审阅" if status_value == "awaiting_review" else "运行完成")
        except Exception as exc:
            failed = stage_name(req.stage); stage_states = dict((store.task(task_id) or {}).get("stageStates", {})); stage_states[failed] = "failed"
            safe_error = store.redact(f"{type(exc).__name__}: {exc}")
            store.save_task(task_id, status="failed", currentStage=failed, failedStage=failed, stageStates=stage_states, error=safe_error)
            store.event(task_id, stage=failed, type="run_failed", status="failed", percent=0, message=f"{failed} 失败")

    @app.post("/api/v1/prds")
    async def upload_prd(file: Any = File(...)):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in ALLOWED_PRD: raise HTTPException(400, "仅支持 Markdown/TXT 文件")
        content = (await file.read()).decode("utf-8-sig")
        prd_id = f"prd-{uuid.uuid4().hex[:12]}"; path = store.prds / f"{prd_id}{suffix}"
        atomic_write_text(path, content); meta = {"prdId": prd_id, "filename": file.filename, "path": str(path.relative_to(root)), "version": "v0.1", "sha256": _content_hash(content), "chars": len(content), "createdAt": now_iso()}; store.save_json(store.prds / f"{prd_id}.json", meta); return meta

    @app.get("/api/v1/prds")
    def list_prds(): return sorted((store._json(p) for p in store.prds.glob("prd-*.json")), key=lambda item:item.get("createdAt", ""), reverse=True)
    @app.get("/api/v1/prds/{prd_id}")
    def get_prd(prd_id: str):
        require_id(prd_id, "prdId")
        meta = store._json(store.prds / f"{prd_id}.json", None)
        if not meta: raise HTTPException(404, "PRD 不存在")
        path = store._safe(store.root / meta["path"])
        versions = sorted((store._json(p) for p in store.prds.glob("prd-*.json") if store._json(p).get("filename") == meta.get("filename")), key=lambda item:item.get("createdAt", ""))
        return {**meta, "content": path.read_text(encoding="utf-8"), "versions": [{"prdId": item.get("prdId"), "version": item.get("version"), "sha256": item.get("sha256")} for item in versions]}
    @app.put("/api/v1/prds/{prd_id}")
    async def update_prd(prd_id: str, body: dict):
        require_id(prd_id, "prdId")
        meta = store._json(store.prds / f"{prd_id}.json", None)
        if not meta or not isinstance(body.get("content"), str): raise HTTPException(400, "PRD 或 content 无效")
        content = body["content"]; suffix = Path(meta["path"]).suffix; new_id = f"prd-{uuid.uuid4().hex[:12]}"; path = store.prds / f"{new_id}{suffix}"; atomic_write_text(path, content)
        new_meta = {**meta, "prdId": new_id, "parentPrdId": prd_id, "path": str(path.relative_to(root)), "version": _next_version(meta.get("version", "v0.1")), "sha256": _content_hash(content), "chars": len(content), "createdAt": now_iso(), "updatedAt": now_iso()}; store.save_json(store.prds / f"{new_id}.json", new_meta); return new_meta

    @app.post("/api/v1/prds/{prd_id}/reanalyze")
    def reanalyze_prd(prd_id: str, body: dict | None = None):
        require_id(prd_id, "prdId")
        meta = store._json(store.prds / f"{prd_id}.json", None)
        if not meta: raise HTTPException(404, "PRD 不存在")
        body = body or {}; task_id = str(uuid.uuid4()); session_id = body.get("sessionId")
        session = store.get_session(str(session_id)) if session_id else None
        if session_id and not session: raise HTTPException(401, "LLM 会话已过期，请重新配置")
        req = build_request(meta, body, task_id)
        public_llm = {"provider":"stub","model":"stub","apiBase":"local"} if req.stub else ({"provider":session["provider"],"model":session["model"],"apiBase":_safe_address(session["apiBase"])} if session else {"provider":base_config.provider,"model":base_config.model,"apiBase":_safe_address(base_config.api_base)})
        store.save_task(task_id, status="pending", currentStage="queued", percent=0, prdId=prd_id, sessionId=session_id, mode="stub" if req.stub else "real", llm=public_llm, request=request_dict(req), stageStates={"analysis":"pending","plan":"pending","cases":"pending"}, review={}, parentRunId=body.get("parentRunId"))
        executor.submit(run_task, task_id, req, session_id, True); return store.task(task_id)

    @app.post("/api/v1/sessions/llm-config")
    def llm_session(body: dict):
        provider = str(body.get("provider", "openai")).lower()
        if provider not in {"openai", "anthropic", "ollama"}: raise HTTPException(400, "Provider 不支持")
        defaults = Config.from_env(provider)
        model = body.get("model") or defaults.model; api_base = (body.get("apiBase") or defaults.api_base).rstrip("/")
        sid = store.put_session({"provider": provider, "model": model, "apiBase": api_base, "apiKey": body.get("apiKey", ""), "temperature": body.get("temperature", defaults.temperature), "timeout": body.get("timeout", defaults.timeout)})
        return {"sessionId": sid, "provider": provider, "model": model, "apiBase": _safe_address(api_base), "expiresIn": 3600}

    @app.post("/api/v1/runs", status_code=202)
    def create_run(body: dict):
        prd_id = str(body.get("prdId", "")); meta = store._json(store.prds / f"{prd_id}.json", None)
        if not meta: raise HTTPException(400, "prdId 不存在")
        session = store.get_session(str(body.get("sessionId"))) if body.get("sessionId") else None
        if body.get("sessionId") and not session: raise HTTPException(401, "LLM 会话已过期，请重新配置")
        task_id = str(uuid.uuid4())
        # auto（一键）：一条任务以 stage="all" 跑完 分析→方案→用例（跳过每阶段人工审阅），
        # 与手动流程（stage="1" 逐阶段审阅批准）互不干扰，任务级 stageStates/review 另行标注。
        auto = bool(body.get("auto", False))
        req = build_request(meta, body, task_id, stage="all" if auto else "1")
        public_llm = {"provider":"stub","model":"stub","apiBase":"local"} if req.stub else ({"provider": session["provider"], "model": session["model"], "apiBase": _safe_address(session["apiBase"])} if session else {"provider": base_config.provider, "model": base_config.model, "apiBase": _safe_address(base_config.api_base)})
        store.save_task(task_id, status="pending", currentStage="queued", percent=0, prdId=prd_id, sessionId=body.get("sessionId"), mode="stub" if req.stub else "real", auto=auto, llm=public_llm, request=request_dict(req), stageStates={"analysis":"pending","plan":"pending","cases":"pending"}, review={}); executor.submit(run_task, task_id, req, body.get("sessionId"), not auto); return store.task(task_id)
    @app.get("/api/v1/runs")
    def list_runs(): return store.list_tasks()
    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str):
        require_id(run_id, "runId")
        data = store.task(run_id)
        if not data: raise HTTPException(404, "运行不存在")
        data["events"] = store.event_list(run_id)[-100:]; return data
    @app.get("/api/v1/runs/{run_id}/events")
    def get_events(run_id: str): return store.event_list(run_id)

    def draft_path(run_id: str, stage: str):
        if stage not in STAGES: raise HTTPException(400, "stage 必须为 analysis、plan 或 cases")
        return root / "drafts" / run_id / f"{stage}.json"
    @app.put("/api/v1/runs/{run_id}/stages/{stage}")
    def save_draft(run_id: str, stage: str, body: dict):
        task = store.task(run_id)
        if not task: raise HTTPException(404, "运行不存在")
        content_hash = _content_hash(json.dumps(body,ensure_ascii=False,sort_keys=True))
        version_dir = root / "drafts" / run_id / stage
        version = len(list(version_dir.glob("draft-*.json"))) + 1
        payload = {"schemaVersion":"1.0","runId":run_id,"stage":stage,"draftVersion":version,
                   "content":body,"savedAt":now_iso(),"contentHash":content_hash}
        store.save_json(version_dir / f"draft-{version:04d}.json", payload)
        store.save_json(draft_path(run_id, stage), payload); store.event(run_id, stage=stage, type="draft_saved", status=task.get("status"), percent=task.get("percent",0), message=f"草稿 v{version} 已保存"); return payload

    def apply_draft(task: dict, stage: str):
        path = draft_path(task["taskId"], stage)
        if not path.exists(): return
        content = store._json(path).get("content", {}); output = Path(task["output"])
        if stage == "analysis":
            markdown = content.get("markdown")
            requirements = content.get("requirements")
            if markdown is not None: write_stage(output, "analysis", str(markdown))
            if requirements is not None:
                if not isinstance(requirements, list) or any(not r.get("id") or not r.get("module") for r in requirements): raise HTTPException(400, "结构化需求缺少 id/module")
                current_md = (output/"analysis"/"需求分析报告.md").read_text(encoding="utf-8")
                save_requirements(output/"analysis"/"requirements.json", requirements, stages.modules_from_analysis(current_md, requirements))
        elif stage == "plan":
            if not isinstance(content.get("markdown"), str): raise HTTPException(400, "方案草稿缺少 markdown")
            write_stage(output, "plan", content["markdown"])
        else:
            cases = content.get("cases")
            if not isinstance(cases, list): raise HTTPException(400, "用例草稿缺少 cases")
            seen=set(); allowed={str(c.get("module")) for c in cases}; errors=[e for i,c in enumerate(cases) for e in validate_case(c,i,seen,allowed)]
            reqs=store._json(output/"analysis"/"requirements.json").get("requirements",[]); matrix=build_traceability(reqs,cases); errors += validate_traceability(matrix)
            if errors: raise HTTPException(400, errors)
            save_cases(output/"cases"/"cases",cases); save_traceability(output/"cases"/"traceability.json",matrix); write_stage(output,"cases",render_cases_md(cases))

    @app.post("/api/v1/runs/{run_id}/stages/{stage}/approve")
    def approve_stage(run_id: str, stage: str, body: dict | None = None):
        task = store.task(run_id)
        if not task: raise HTTPException(404, "运行不存在")
        if task.get("status") != "awaiting_review" or task.get("currentStage") != stage: raise HTTPException(409, "当前阶段不可批准")
        if body: save_draft(run_id, stage, body)
        apply_draft(task, stage)
        latest = store._json(draft_path(run_id, stage), {})
        review=dict(task.get("review",{})); review[stage]={"status":"approved","approvedBy":"local-user","approvedAt":now_iso(),"contentHash":latest.get("contentHash")}; states=dict(task.get("stageStates",{})); states[stage]="approved"
        if stage == "cases":
            store.save_task(run_id,status="succeeded",currentStage="done",stageStates=states,review=review); store.event(run_id,stage=stage,type="stage_approved",status="succeeded",percent=100,message="用例已批准"); return store.task(run_id)
        next_stage = "2" if stage == "analysis" else "3"; next_name=stage_name(next_stage); states[next_name]="pending"
        store.save_task(run_id,status="pending",currentStage=next_name,stageStates=states,review=review); req=request_from(task["request"],next_stage,True); executor.submit(run_task,run_id,req,task.get("sessionId"),True); return store.task(run_id)

    @app.post("/api/v1/runs/{run_id}/stages/{stage}/reject")
    def reject_stage(run_id: str, stage: str, body: dict):
        task=store.task(run_id)
        if not task or task.get("currentStage") != stage: raise HTTPException(409,"当前阶段不可驳回")
        reason=str(body.get("reason","")).strip()
        if not reason: raise HTTPException(400,"驳回原因不能为空")
        review=dict(task.get("review",{})); review[stage]={"status":"rejected","reason":reason,"rejectedAt":now_iso()}; states=dict(task.get("stageStates",{})); states[stage]="rejected"
        data=store.save_task(run_id,status="rejected",stageStates=states,review=review); store.event(run_id,stage=stage,type="stage_rejected",status="rejected",percent=task.get("percent",0),message=reason); return data

    @app.post("/api/v1/runs/{run_id}/modules/{module}/retry")
    def retry_module(run_id: str, module: str):
        task=store.task(run_id)
        if not task or not task.get("request"): raise HTTPException(404,"运行不存在")
        output_state = store._json(Path(task.get("output", "")) / "run-state.json", {}) if task.get("output") else {}
        modules = output_state.get("modules", {})
        if module not in modules: raise HTTPException(404, "模块不存在")
        if modules.get(module) != "failed": raise HTTPException(409, "仅失败模块可重试")
        req=request_from(task["request"],"3",True); store.save_task(run_id,status="pending",currentStage="cases",retryModule=module); executor.submit(run_task,run_id,req,task.get("sessionId"),True); return store.task(run_id)
    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: str): return store.save_task(run_id, status="cancelled", currentStage="cancelled")
    @app.post("/api/v1/runs/{run_id}/resume")
    def resume_run(run_id: str):
        task = store.task(run_id)
        if not task: raise HTTPException(404, "运行不存在")
        failed=task.get("failedStage") or task.get("currentStage")
        stage={"analysis":"1","plan":"2","cases":"3"}.get(failed)
        if not stage: raise HTTPException(409,"当前状态不可恢复")
        req=request_from(task["request"],stage,True); store.save_task(run_id,status="pending",currentStage=failed); executor.submit(run_task,run_id,req,task.get("sessionId"),True); return store.task(run_id)
    @app.get("/api/v1/runs/{run_id}/artifacts")
    def artifacts(run_id: str):
        task = store.task(run_id)
        if not task or not task.get("output"): raise HTTPException(404, "运行产物不存在")
        output = Path(task["output"]).resolve(); files = []
        if root not in output.parents and output != root: raise HTTPException(400, "非法产物路径")
        for p in output.rglob("*"):
            if p.is_file() and ".checkpoints" not in p.parts: files.append({"path": str(p.relative_to(output)).replace("\\", "/"), "size": p.stat().st_size, "contentType": mimetypes.guess_type(p.name)[0] or "application/octet-stream"})
        return files
    @app.get("/api/v1/runs/{run_id}/artifacts/{artifact_path:path}")
    def artifact(run_id: str, artifact_path: str):
        task = store.task(run_id); output = Path(task.get("output", "")).resolve() if task else Path()
        target = (output / artifact_path).resolve()
        if not task or output not in target.parents or not target.is_file(): raise HTTPException(404, "产物不存在")
        if target.suffix in {".json"}: return JSONResponse(json.loads(target.read_text(encoding="utf-8")))
        return PlainTextResponse(target.read_text(encoding="utf-8"))
    @app.websocket("/api/v1/runs/{run_id}/events")
    @app.websocket("/api/v1/runs/{run_id}/events/ws")
    async def events_ws(websocket: WebSocket, run_id: str):
        import asyncio
        await websocket.accept(); sent = 0
        while True:
            events = store.event_list(run_id)
            for event in events[sent:]: await websocket.send_json(event)
            sent = len(events); task = store.task(run_id)
            if task and task.get("status") in {"succeeded", "failed", "cancelled"}: await websocket.close(); return
            await asyncio.sleep(.5)
    @app.on_event("shutdown")
    def shutdown(): executor.shutdown(wait=False, cancel_futures=True)

    @app.post("/runs", status_code=202)
    def legacy_create_run(body: dict):
        prd = Path(str(body.get("prd", "")))
        if not prd.is_file(): raise HTTPException(400, "prd 必须指向可读取文件")
        task_id = str(uuid.uuid4()); req = GenerateRequest(prd=prd, out=body.get("out", "output"), product=body.get("product"), version=body.get("version"), stub=bool(body.get("stub", False)), jobs=int(body.get("jobs", 1)), stage=body.get("stage", "all"))
        store.save_task(task_id, status="pending", currentStage="queued", percent=0, request=request_dict(req), stageStates={}); executor.submit(run_task, task_id, req, None, False); return store.task(task_id)
    @app.get("/runs/{run_id}")
    def legacy_get_run(run_id: str):
        data = store.task(run_id)
        if not data:
            spa_index = (Path(ui_dir).resolve() if ui_dir else Path(__file__).resolve().parents[2] / "ui" / "dist") / "index.html"
            if spa_index.is_file(): return FileResponse(spa_index)
            raise HTTPException(404, "任务不存在")
        return data

    static_dir = Path(ui_dir).resolve() if ui_dir else Path(__file__).resolve().parents[2] / "ui" / "dist"
    if static_dir.is_dir():
        from fastapi.staticfiles import StaticFiles
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir(): app.mount("/assets", StaticFiles(directory=assets_dir), name="ui-assets")
        index_file = static_dir / "index.html"
        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str):
            candidate = (static_dir / full_path).resolve()
            if candidate.is_file() and static_dir in candidate.parents: return FileResponse(candidate)
            return FileResponse(index_file)
    return app
