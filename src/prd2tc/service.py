"""可选 FastAPI 适配层：文件工作区、阶段审阅和实时事件。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib, json, mimetypes, re, secrets, shutil, threading, time, uuid
from pathlib import Path
from typing import Any

from prd2tc import __version__, stages
from prd2tc import ingest
from prd2tc.config import Config
from prd2tc.export.common import atomic_write_bytes
from prd2tc.io import (atomic_write_text, now_iso, save_cases, save_requirements,
                       save_traceability, write_stage, render_cases_md)
from prd2tc.llm import LLMCancelled, probe_provider
from prd2tc.pipeline import GenerateRequest, PipelineRunner
from prd2tc.traceability import build_traceability, validate_traceability
from prd2tc.validator import format_exception, validate_case

ALLOWED_PRD = {".md", ".markdown", ".mdown", ".txt", ".text",
               ".docx", ".docm", ".pdf", ".zip",
               ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_UPLOAD_FILES = 400
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 256 * 1024 * 1024


def _safe_filename(name: str) -> str:
    """清洗上传文件名：只保留基名，剔除路径分隔与危险字符。

    上传的文件名可被客户端任意构造（含 `../`），直接落盘会造成目录穿越。
    """
    base = Path(str(name or "")).name.strip() or "upload.bin"
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base)
    base = base.lstrip(".") or "upload.bin"
    return base[:120]


def _safe_relpath_parts(relpath: str) -> list[str]:
    """把上传的相对路径拆成**安全的目录段**（用于在磁盘上还原目录结构）。

    为什么要还原结构：若把上传的文件全部拍平到同一层，那么
    「根目录的 PRD」与「子目录里的附件」在磁盘上就不再可区分，
    摄取层赖以判定主文件的 `depth` 会全部退化为 0
    （实测：`图片和附件/rag-service.md` 因此与 PRD 同级，层级线索失效）。

    安全：逐段清洗，丢弃 `.`/`..`/盘符/空段，杜绝目录穿越。
    """
    parts = []
    for raw in Path(str(relpath or "")).parts:
        if raw in (".", "..", "", "/", "\\") or raw.endswith(":") or ":" in raw:
            continue
        cleaned = _safe_filename(raw)
        if cleaned and cleaned not in (".", ".."):
            parts.append(cleaned)
    return parts[:-1]      # 最后一段是文件名本身
STAGES = ("analysis", "plan", "cases")
# 这些产物是二进制，不能用 read_text 读取
BINARY_ARTIFACT_SUFFIXES = {".xlsx", ".xmind", ".zip", ".png", ".pdf"}
# mimetypes 不认识 .xmind，注册后 artifact 接口才能给出正确 Content-Type
mimetypes.add_type("application/vnd.xmind.workbook", ".xmind")

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
        self.live: dict[str, Any] = {}       # taskId -> LiveMetrics（内存态实时指标）
        self.runners: dict[str, Any] = {}    # taskId -> PipelineRunner（用于取消止损）
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
        # 读取同样要加锁：心跳/进度线程在写 task.json 与事件文件时，
        # 无锁读会在 Windows 上撞上 os.replace 而抛 PermissionError
        with self._lock:
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
        with self._lock:
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
        from typing import List
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
        prd_path = root / meta["path"]
        # 上传目录里的其它文件（图片/附件/子目录）一并作为输入：
        # 这样「PRD + 图片和附件/」从 UI 上传即可直接生成，无需 --images-dir 绕道。
        # 必须用 rglob 递归：目录结构已按相对路径还原，附件常在子目录里。
        extra: list = []
        prd_dir = meta.get("dir")
        if prd_dir:
            folder = root / prd_dir
            if folder.is_dir():
                extra = [p for p in sorted(folder.rglob("*"))
                         if p.is_file() and p != prd_path and p.suffix.lower() != ".json"]
        return GenerateRequest(
            prd=([prd_path] + extra) if extra else prd_path,
            out=root / "outputs" / task_id, product=product,
            version=version, stub=bool(body.get("stub", False)),
            jobs=max(1, int(body.get("jobs") or base_config.jobs or 1)),
            profile=body.get("profile", "generic"), stage=stage,
            max_gate_retries=int(body.get("maxGateRetries", 2)),
            allow_gate_fail=bool(body.get("allowGateFail", False)), no_gate=bool(body.get("noGate", False)))

    def stage_name(value: str): return {"1": "analysis", "2": "plan", "3": "cases", "all": "done"}.get(value, "unknown")
    def request_dict(req: GenerateRequest):
        return {"prd": str(req.prd), "out": str(req.out), "product": req.product, "version": req.version,
                "stub": req.stub, "jobs": req.jobs, "profile": req.profile,
                "max_gate_retries": req.max_gate_retries, "allow_gate_fail": req.allow_gate_fail,
                "no_gate": req.no_gate}
    def require_stage(value: object) -> str:
        if value not in {"1", "2", "3", "all"}:
            raise HTTPException(400, f"stage 必须为 1/2/3/all，收到 {value!r}")
        return str(value)

    def request_from(data: dict, stage: str, resume=True):
        return GenerateRequest(**data, stage=require_stage(stage), resume=resume)

    def run_task(task_id: str, req: GenerateRequest, session_id: str | None = None, review_mode: bool = True):
        session = store.get_session(session_id) if session_id else None
        runner_config = base_config
        if session:
            runner_config = replace(base_config, provider=session["provider"], model=session["model"], api_base=session["apiBase"], api_key=session.get("apiKey", ""), temperature=float(session.get("temperature", base_config.temperature)), timeout=int(session.get("timeout", base_config.timeout)))
        try:
            current = stage_name(req.stage); task = store.task(task_id) or {}; stage_states = dict(task.get("stageStates", {})); stage_states[current] = "running"
            store.save_task(task_id, status="running", currentStage=current, failedStage=None, stageStates=stage_states)
            store.event(task_id, stage=current, type="run_started", status="running", percent=task.get("percent", 0), message=f"开始 {current}")
            runner = PipelineRunner(runner_config)
            store.runners[task_id] = runner; store.live[task_id] = runner.live
            heartbeat = threading.Event()

            def progress(stage, percent):
                # 已取消时不得把状态改回 running（否则取消会被覆盖、运行继续烧 token）
                if (store.task(task_id) or {}).get("status") == "cancelled":
                    runner.cancel(); return
                store.save_task(task_id, status="running", currentStage=stage, percent=percent)
                store.event(task_id, stage=stage, type="stage_progress", status="running",
                            percent=percent, message=f"{stage} 进度 {percent}%")

            def on_event(payload: dict):
                """P0/P1 结构化事件：模块/批次/阶段开始，走独立通道，不污染阶段进度语义。"""
                fields = dict(payload or {})
                kind = str(fields.pop("type", "stage_progress"))
                module = fields.pop("module", None)
                fields.pop("percent", None)
                detail = " · ".join(f"{k}={v}" for k, v in fields.items() if k in {"batch", "state", "tokens"})
                store.event(task_id, stage=str(fields.get("stage") or current), type=kind,
                            status="running", percent=(store.task(task_id) or {}).get("percent", 0),
                            module=module, message=f"{module or current} {kind}{(' · ' + detail) if detail else ''}")

            def beat():
                """心跳：长耗时期间也持续发事件，避免 UI 静默误判为卡死。"""
                while not heartbeat.wait(5.0):
                    snapshot = runner.live.snapshot(force=True) or {}
                    store.event(task_id, stage=str(snapshot.get("stage") or current), type="heartbeat",
                                status="running", percent=(store.task(task_id) or {}).get("percent", 0),
                                message=f"静默 {int((snapshot.get('silentMs') or 0) / 1000)}s · "
                                        f"已用 {int((snapshot.get('elapsedMs') or 0) / 1000)}s")
                    if (store.task(task_id) or {}).get("status") in {"succeeded", "failed", "cancelled", "awaiting_review"}:
                        return

            beat_thread = threading.Thread(target=beat, name=f"heartbeat-{task_id[:8]}", daemon=True)
            beat_thread.start()
            try:
                result = runner.generate(req, progress_callback=progress, event_callback=on_event)
            finally:
                heartbeat.set()
                runner.live.final_snapshot()
                store.runners.pop(task_id, None)
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
        except LLMCancelled:
            # 用户取消不是失败：落 cancelled，并保留已产出的部分进度
            store.save_task(task_id, status="cancelled", currentStage="cancelled",
                            stageStates=dict((store.task(task_id) or {}).get("stageStates", {})))
            store.event(task_id, type="run_cancelled", status="cancelled", percent=0, message="任务已取消（流式响应已停止）")
        except Exception as exc:
            failed = stage_name(req.stage); stage_states = dict((store.task(task_id) or {}).get("stageStates", {})); stage_states[failed] = "failed"
            safe_error = store.redact(format_exception(exc))
            errors = [store.redact(str(e)) for e in (getattr(exc, "errors", None) or [safe_error])]
            store.save_task(task_id, status="failed", currentStage=failed, failedStage=failed,
                            stageStates=stage_states, error=safe_error, errors=errors)
            store.event(task_id, stage=failed, type="run_failed", status="failed", percent=0, message=f"{failed} 失败")

    @app.post("/api/v1/prds")
    async def upload_prd(file: List[UploadFile] = File(None), files: List[UploadFile] = File(None)):
        """上传 PRD：支持**多文件**与**多格式**（md/txt/docx/pdf/zip/图片）。

        改动原因：原先只接受单个 `.md/.txt` 且强制 UTF-8 解码，
        于是 `.docx`（PRD 最常见交付格式）、Wiki 导出的 `.zip`
        （md + 图片 + 附件）、以及「需求本身就是截图」的多模态输入全被 400 拒绝。

        参数兼容：`files`（多文件，推荐）与 `file`（单文件，向后兼容）取并集。
        类型必须声明为 `List[UploadFile]`：用 `Any` 时 FastAPI 不按列表解析会返 422。

        每个文件**保真落盘**（原始字节，不做文本化）：
        图片与 docx 的二进制一旦被当文本处理就毁了；格式判定与解析交给 `ingest`。
        """
        items = list(file or []) + list(files or [])
        if not items:
            raise HTTPException(400, "未上传任何文件")
        if len(items) > MAX_UPLOAD_FILES:
            raise HTTPException(400, f"一次最多上传 {MAX_UPLOAD_FILES} 个文件")

        prd_id = f"prd-{uuid.uuid4().hex[:12]}"
        folder = store.prds / prd_id
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[dict] = []
        payloads: list[tuple] = []
        total = 0
        for item in items:
            # 文件夹上传时浏览器会带上相对路径；保留它才能还原层级，
            # 进而正确判定「哪个才是 PRD 正文」（根目录的 PRD 胜过子目录的附件）。
            relpath = str(getattr(item, "filename", "") or "upload.bin")
            name = _safe_filename(Path(relpath).name or relpath)
            data = await item.read()
            if not data:
                continue
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(400, f"{name} 超过单文件上限 "
                                         f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB")
            total += len(data)
            if total > MAX_UPLOAD_TOTAL_BYTES:
                raise HTTPException(400, f"上传总量超过 "
                                         f"{MAX_UPLOAD_TOTAL_BYTES // 1024 // 1024}MB，请分批")
            # **按相对路径还原目录结构**（而非拍平）：保留层级，
            # 摄取层才能区分「根目录的 PRD」与「子目录里的附件」。
            folder.mkdir(parents=True, exist_ok=True)
            target = folder.joinpath(*_safe_relpath_parts(relpath), name)
            target.parent.mkdir(parents=True, exist_ok=True)
            # 同名文件去重（多选/深层目录可能撞名），保证不互相覆盖
            seq = 1
            while target.exists():
                target = target.with_name(f"{Path(name).stem}-{seq}{Path(name).suffix}")
                seq += 1
            atomic_write_bytes(target, data)
            # 传 relpath 给摄取层：depth 由它推导，用于主文件判定
            payloads.append((target.name, data, "upload", str(target.relative_to(folder))))
            saved.append({"name": target.name, "size": len(data), "relpath": relpath,
                          "stored": str(target.relative_to(folder)),
                          "sha256": hashlib.sha256(data).hexdigest()})

        if not saved:
            raise HTTPException(400, "上传内容为空")

        # 用摄取层做一次真实解析：立刻把「哪些是正文、哪些是资产、有哪些告警」
        # 反馈给前端，避免用户上传后才发现格式不被支持。
        try:
            bundle = ingest.ingest(payloads)
            summary = bundle.summary()
            warnings_list = list(bundle.warnings)
            primary = bundle.primary
        except (ValueError, OSError) as exc:
            raise HTTPException(400, f"无法解析上传内容：{exc}") from exc

        primary_name = next((s["name"] for s in saved if s["name"] == primary), saved[0]["name"])
        # 主文件在磁盘上的**实际相对路径**（可能位于还原出的子目录里，
        # 如 `我的PRD/PRD.md`）。若这里只用 basename，`path` 会指向不存在的文件，
        # 后续运行就找不到显式主文件、层级信息也随之失效（实测踩到）。
        primary_stored = next((s.get("stored") or s["name"] for s in saved
                               if s["name"] == primary_name), primary_name)

        # --- 内容去重（第二道防线）---
        # 前端曾因 antd 逐文件回调而把一次文件夹上传发成 N 次请求，
        # 列表里堆了 8 条同名同版本记录。仅修前端不足以防御
        # 「用户连点两次」「脚本重复调用」等情况，故在此按**内容**去重：
        # 文件集合的 sha256（排序后）完全相同即视为同一次上传，复用已有记录。
        # 注意：`PUT /prds/{id}`（保存新版本）是用户显式行为，不经此路径。
        content_digest = _content_hash("\n".join(sorted(s["sha256"] for s in saved)))
        for existing_path in sorted(store.prds.glob("prd-*.json")):
            existing = store._json(existing_path)
            if existing.get("prdId") == prd_id:
                continue
            # 只按 contentDigest 匹配：旧记录没有该字段（其 sha256 语义不同），
            # 不设兼容分支可避免把「不同内容但哈希巧合」误判为重复。
            if existing.get("contentDigest") == content_digest:
                shutil.rmtree(folder, ignore_errors=True)      # 丢弃本次重复落盘
                return {**existing, "deduplicated": True}

        meta = {
            "prdId": prd_id,
            # filename 保持主文件名（下游按它生成产品名）；files 给出完整清单
            "filename": primary_name,
            "path": str((folder / primary_stored).relative_to(root)),
            "dir": str(folder.relative_to(root)),
            "files": saved,
            "primary": primary_name,
            "contentDigest": content_digest,
            "bundle": summary,
            "warnings": warnings_list,
            "version": "v0.1",
            "sha256": _content_hash("\n".join(s["sha256"] for s in saved)),
            "chars": summary["textChars"],
            "createdAt": now_iso(),
        }
        store.save_json(store.prds / f"{prd_id}.json", meta)
        return meta

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
        content = body["content"]; suffix = Path(meta["path"]).suffix
        new_id = f"prd-{uuid.uuid4().hex[:12]}"
        # 与上传保持一致：新版本也放进自己的目录，避免文件散落在 prds/ 根下
        # （上传已改为 `prds/<id>/`，两处不一致会让目录结构难以理解）。
        folder = store.prds / new_id
        folder.mkdir(parents=True, exist_ok=True)
        # 主文件在旧目录里的**相对位置**（如 `我的PRD/PRD.md`），
        # 新正文必须写回同一位置 —— 否则会出现「旧副本 + 新扁平副本」两份 PRD。
        old_folder = store._safe(store.root / meta["dir"]) if meta.get("dir") else None
        old_main = ""
        if meta.get("path") and meta.get("dir"):
            try:
                old_main = str((store.root / meta["path"]).relative_to(store.root / meta["dir"]))
            except ValueError:
                old_main = Path(meta["filename"]).name
        # **把上一版的其余文件（图片/附件/子目录）复制过来**：
        # 新版本只编辑正文文本，资产应当延续。否则新版本目录里只剩主文件，
        # 后续运行读不到配图，而 `files` 元数据却仍声称有 N 个文件（实测踩到）。
        # 跳过旧主文件本身 —— 它马上会被新内容替换，不跳会出现两份。
        if old_folder and old_folder.is_dir():
            for src in sorted(old_folder.rglob("*")):
                if not src.is_file() or src.name.endswith(".json"):
                    continue
                rel = src.relative_to(old_folder)
                if str(rel) == old_main:
                    continue
                dest = folder / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dest)
                except OSError:
                    pass
        path = folder / (old_main or Path(meta["filename"]).name)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content)
        # 重建 files 清单：主文件用新哈希，其余沿用复制过来的实际内容
        files: list[dict] = []
        for item in sorted(folder.rglob("*")):
            if not item.is_file() or item.name.endswith(".json"):
                continue
            blob = item.read_bytes()
            files.append({"name": item.name, "size": len(blob),
                          "relpath": str(item.relative_to(folder)),
                          "stored": str(item.relative_to(folder)),
                          "sha256": hashlib.sha256(blob).hexdigest()})
        content_digest = _content_hash("\n".join(sorted(f["sha256"] for f in files)))
        new_meta = {**meta, "prdId": new_id, "parentPrdId": prd_id,
                    "path": str(path.relative_to(root)), "dir": str(folder.relative_to(root)),
                    "files": files or meta.get("files") or [],
                    "version": _next_version(meta.get("version", "v0.1")),
                    "sha256": _content_hash(content), "chars": len(content),
                    # 新版本有自己的内容指纹；不继承父记录（否则后续上传会被误判重复）
                    "contentDigest": content_digest,
                    "updatedAt": now_iso(), "createdAt": now_iso()}
        store.save_json(store.prds / f"{new_id}.json", new_meta); return new_meta

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
        candidate = replace(defaults, provider=provider, model=model, api_base=api_base,
                            api_key=str(body.get("apiKey", "") or ""),
                            temperature=float(body.get("temperature", defaults.temperature) or defaults.temperature),
                            timeout=int(body.get("timeout", defaults.timeout) or defaults.timeout))
        probe = probe_provider(candidate)
        if not probe.ok:
            # 保存前拒绝带病配置：否则错误要到运行阶段才以无信息量的形式暴露
            raise HTTPException(400, detail={"notice": "LLM 配置检测未通过，已拒绝保存",
                                             **probe.to_dict()})
        sid = store.put_session({"provider": provider, "model": probe.model or model, "apiBase": api_base,
                                 "apiKey": body.get("apiKey", ""), "temperature": candidate.temperature,
                                 "timeout": candidate.timeout})
        return {"sessionId": sid, "provider": provider, "model": probe.model or model,
                "apiBase": _safe_address(api_base), "expiresIn": 3600, "probe": probe.to_dict()}

    @app.post("/api/v1/sessions/llm-config/verify")
    def verify_llm_config(body: dict):
        """只探测不保存：供 UI「检测连接」按钮复用，不写入任何会话状态。"""
        provider = str(body.get("provider", "openai")).lower()
        if provider not in {"openai", "anthropic", "ollama"}: raise HTTPException(400, "Provider 不支持")
        defaults = Config.from_env(provider)
        candidate = replace(defaults, provider=provider,
                            model=body.get("model") or defaults.model,
                            api_base=(body.get("apiBase") or defaults.api_base).rstrip("/"),
                            api_key=str(body.get("apiKey", "") or ""),
                            timeout=int(body.get("timeout", defaults.timeout) or defaults.timeout))
        return probe_provider(candidate).to_dict()

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
        live = store.live.get(run_id)
        if live is not None:
            snapshot = live.snapshot(force=True)
            if snapshot is not None: data["metrics"] = {**snapshot, "runId": run_id}
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
    def cancel_run(run_id: str):
        # 真正止损：置取消标志，流式回调命中后立即停止接收响应
        runner = store.runners.get(run_id)
        if runner is not None: runner.cancel()
        store.event(run_id, type="run_cancelled", status="cancelled", percent=0, message="已请求取消")
        return store.save_task(run_id, status="cancelled", currentStage="cancelled")
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
        if target.suffix == ".json": return JSONResponse(json.loads(target.read_text(encoding="utf-8")))
        # 二进制产物（.xlsx / .xmind 等）不能用 read_text 读取，否则抛 UnicodeDecodeError；
        # 按扩展名区分，二进制走 FileResponse 以便浏览器直接下载/打开。
        if target.suffix.lower() in BINARY_ARTIFACT_SUFFIXES:
            return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0]
                                or "application/octet-stream", filename=target.name)
        return PlainTextResponse(target.read_text(encoding="utf-8"))

    @app.get("/api/v1/exports/{run_id}/{fmt}")
    def download_export(run_id: str, fmt: str):
        """按需生成并返回导出文件（fmt: excel | xmind）。

        输出目录固定在运行产物内，不接受外部路径参数，因此无需 _safe 校验（无穿越面）。
        """
        if fmt not in {"excel", "xmind"}: raise HTTPException(400, f"不支持的格式: {fmt}")
        require_id(run_id, "runId")
        task = store.task(run_id)
        if not task: raise HTTPException(404, "运行不存在")
        output = Path(task.get("output") or "").resolve()
        cases_dir = output / "cases" / "cases"
        if not cases_dir.is_dir(): raise HTTPException(404, "该运行尚无用例产物")
        from prd2tc.export import export_cases, MEDIA_TYPES
        try:
            result = export_cases(cases_dir=cases_dir, formats=[fmt], out=None, root=output)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(400, format_exception(exc)) from exc
        path = Path(result["files"][0])
        return FileResponse(path, media_type=MEDIA_TYPES[fmt], filename=path.name)
    @app.websocket("/api/v1/runs/{run_id}/events")
    @app.websocket("/api/v1/runs/{run_id}/events/ws")
    async def events_ws(websocket: WebSocket, run_id: str):
        import asyncio
        # ?metrics=1 才推送 run_metrics：旧客户端/旧测试不会收到未知消息类型
        wants_metrics = str(websocket.query_params.get("metrics", "")).lower() in {"1", "true", "yes"}
        await websocket.accept(); sent = 0
        metrics_pushed = False
        while True:
            events = store.event_list(run_id)
            for event in events[sent:]: await websocket.send_json(event)
            sent = len(events)
            task = store.task(run_id)
            terminal = bool(task and task.get("status") in {"succeeded", "failed", "cancelled"})
            if wants_metrics:
                live = store.live.get(run_id)
                if live is not None:
                    # 终态时必须 force：否则限流窗口内返回 None，UI 拿不到收尾快照
                    snapshot = live.snapshot(force=terminal or not metrics_pushed)
                    if snapshot is not None:
                        snapshot["runId"] = run_id
                        await websocket.send_json(snapshot)
                        metrics_pushed = True
            # 关键：终态也必须先把指标推出去再关闭，否则短运行（连接建立前已结束）
            # 会让客户端一帧都收不到，实时面板永远空着。
            if terminal and (not wants_metrics or metrics_pushed):
                await websocket.close(); return
            await asyncio.sleep(.5)
    @app.on_event("shutdown")
    def shutdown(): executor.shutdown(wait=False, cancel_futures=True)

    @app.post("/runs", status_code=202)
    def legacy_create_run(body: dict):
        prd = Path(str(body.get("prd", "")))
        if not prd.is_file(): raise HTTPException(400, "prd 必须指向可读取文件")
        task_id = str(uuid.uuid4()); req = GenerateRequest(prd=prd, out=body.get("out", "output"), product=body.get("product"), version=body.get("version"), stub=bool(body.get("stub", False)), jobs=max(1, int(body.get("jobs") or base_config.jobs or 1)), stage=require_stage(body.get("stage", "all")))
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
