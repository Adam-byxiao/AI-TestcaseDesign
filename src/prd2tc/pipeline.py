"""统一生成编排内核，供 CLI、Python API 和服务层复用。"""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib, json, re, threading, time, uuid, warnings
from pathlib import Path
from typing import Callable

from prd2tc import stages
from prd2tc.config import Config
from prd2tc.io import (MANIFEST_NAME, LEGACY_MANIFEST_NAME, artifact_inventory,
    atomic_write_text, init_output, load_requirements, manifest_stage, render_cases_md,
    save_cases, save_requirements, save_traceability, stats, update_manifest,
    update_run_state, write_stage)
from prd2tc.llm import LLMResult
from prd2tc.metrics import CancelFlag, LiveMetrics
from prd2tc import metrics
from prd2tc import images
from prd2tc import ingest
from prd2tc import prototype
from prd2tc import priority
from prd2tc import artifacts
from prd2tc.profiles import get_profile
from prd2tc.schema import normalize_cases
from prd2tc.traceability import build_traceability, validate_traceability
from prd2tc.validator import ValidationError, format_exception

@dataclass
class GenerateRequest:
    # prd 可为：单个文件 / 目录 / 路径列表 / (name, bytes) 列表（多格式多文件）
    prd: str | Path | list | None = None
    # 已摄取的 Bundle（服务层上传后直接传入，避免重新读盘）
    bundle: object | None = None
    out: str | Path = "output"
    product: str | None = None
    version: str | None = None
    examples: list | None = None
    stub: bool = False
    dry_run: bool = False
    jobs: int = 1
    stage: str = "all"
    # 从该阶段开始**跑到最后**（`--from-stage`）。与 stage 的区别：
    #   stage="2"      → 只跑阶段 2（复用已有上游产物）
    #   from_stage="2" → 跑阶段 2 与 3
    from_stage: str = ""
    # 允许复用「为另一份 PRD 生成」的上游产物（默认禁止：那是静默错配的来源）
    allow_stale_artifacts: bool = False
    profile: str = "generic"
    min_cases_per_requirement: int | None = None
    max_gate_retries: int = 2
    allow_gate_fail: bool = False
    no_gate: bool = False
    resume: bool = False
    force: bool = False
    # 交互式设计产物（原型 HTML 等）：作为 UI 契约真值，供阶段 3 引用与门禁校验
    attachments: list | None = None
    # PRD 图片：目录（默认按约定名自动探测）+ 是否下载远程图片
    image_dirs: list | None = None
    fetch_images: bool = False
    analyze_images: bool = True

@dataclass
class RunResult:
    run_id: str
    root: Path
    cases: list[dict] = field(default_factory=list)
    requirements: list[dict] = field(default_factory=list)
    status: str = "succeeded"
    errors: list[str] = field(default_factory=list)
    prompts: dict[str, str] = field(default_factory=dict)
    statistics: dict = field(default_factory=dict)
    metrics: object | None = None  # LiveMetrics：供服务层推送实时快照

def extract_version(text: str, path: Path) -> str:
    m = re.search(r"版本[：:]\s*(v[\d.]+)", text)
    if m: return m.group(1)
    m = re.search(r"PRD-[^-]+-(v[\d.]+)\.md$", path.name)
    return m.group(1) if m else "v0.1"

def _prd_path(request, bundle) -> Path:
    """推断用于「产品名 / 版本号 / manifest」的 PRD 路径。

    多文件或纯上传场景可能没有真实磁盘路径，此时退回主文件来源名，
    仅用于命名与留痕（不参与内容读取）。
    """
    prd = getattr(request, "prd", None)
    if isinstance(prd, (str, Path)):
        return Path(prd)
    if isinstance(prd, (list, tuple)) and prd:
        first = prd[0]
        if isinstance(first, (str, Path)):
            return Path(first)
    if bundle is not None:
        for src in bundle.sources:
            if src.role == "primary" and src.origin:
                cand = Path(src.origin)
                if cand.exists():
                    return cand
        if bundle.primary:
            return Path(bundle.primary)
    return Path("prd")


class PipelineRunner:
    def __init__(self, config: Config, *, llm=None):
        self.config = config
        self._custom_llm = llm is not None
        self.llm = llm or stages.chat_completion
        # 实时指标在构造期创建，服务层可在 generate() 进行中读取快照（用于 WS 推送）
        self.live = LiveMetrics(push_interval_ms=getattr(config, "stream_push_ms", 500))
        self.cancel_flag = CancelFlag()

    def _load_attachments(self, paths, root: Path):
        """把交互式设计产物（原型 HTML 等）解析为 UI 契约，并落盘留痕。

        为什么必要：真实 PRD 常引用可运行原型，而**页面的真实标识符只存在于原型里**。
        实测运行 c653dc92：PRD 中 `data-testid` 出现 0 次、原型未参与生成，
        模型因此编造 143 个标识符（全部不存在）与 7 种 URL 路径（原型是单页应用、无路由），
        并按三选项弹窗臆造交互（真值只有 1 个「关闭」按钮）。

        失败策略：附件不可解析时**只告警不中断** —— 原型是增强输入，不是硬依赖。
        """
        if not paths:
            return None, []
        specs, metas = [], []
        for raw in paths:
            p = Path(raw)
            try:
                spec = prototype.parse_prototype(p)
            except (FileNotFoundError, ValueError, OSError) as exc:
                warnings.warn(f"附件无法解析为原型，已跳过: {p}（{type(exc).__name__}: {exc}）")
                continue
            specs.append(spec)
            metas.append({"path": str(p), "name": p.name,
                          "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                          **spec.summary()})
        if not specs:
            return None, []
        merged = prototype.merge_specs(specs)
        # 落盘：让产物可追溯「这套标识符是从哪份原型来的」，并供导出/复审引用
        try:
            atomic_write_text(root / "analysis" / "ui-spec.json",
                              json.dumps(prototype.to_json(merged), ensure_ascii=False, indent=2) + "\n")
        except Exception as exc:      # 目录未就绪等：不影响主流程
            warnings.warn(f"UI 契约留痕失败（不影响生成）: {exc}")
        return merged, metas

    def _auto_prototypes(self, bundle, root: Path):
        """从**已摄取的文件**里自动识别原型 HTML 并解析为 UI 契约。

        为什么需要：`--attach` 是 CLI 入口，而 UI 上传文件夹时原型只是被当成
        普通文本附件内联进正文 —— 它携带的真实标识符/页面/弹窗按钮**全都没被利用**，
        模型仍会编造。实测用户上传的文件夹里就有
        `养老Agent后台-prototype-测试内嵌版.html`（48KB），却只作为文本进了上下文
        （既稀释了 PRD 注意力，又没换来任何结构化真值）。

        仅在未显式提供 `--attach` 时启用；解析失败只告警，绝不中断。
        """
        if bundle is None:
            return None, []
        paths: list[Path] = []
        seen: set[str] = set()
        for src in bundle.sources:
            if getattr(src, "kind", "") != "html" or getattr(src, "role", "") == "skipped":
                continue
            origin = str(getattr(src, "origin", "") or "")
            if not origin or origin in seen:
                continue
            p = Path(origin)
            if p.is_file() and p not in paths:
                seen.add(origin)
                paths.append(p)
        if not paths:
            return None, []
        spec, metas = self._load_attachments(paths, root)
        if spec is not None:
            for m in metas:
                m["autoDetected"] = True
        return spec, metas

    def _load_images(self, bundle, root: Path, *, request, llm, reporter=None):
        """从摄取结果构建图片证据，落盘留痕；失败只告警不中断。

        为什么必要：PRD 的**关键 UI 事实常只在图里**（线框图、字段布局、弹窗按钮、
        状态枚举、以及用红框标注的「本次改动点」）。实测真实 PRD 含 8 处图片引用，
        此前全部未参与生成 —— 这正是模型臆造交互（如三选项弹窗）的直接原因。

        图片来自 `bundle.assets`（上传 / 压缩包 / DOCX 内嵌 / 目录），
        因此不再需要「先把图落到某个目录、再用 --images-dir 指过去」的绕道。
        """
        images_in = bundle.images() if bundle is not None else []
        if not images_in:
            return None
        try:
            spec = images.prepare_from_assets(
                images_in, text=bundle.text,
                allow_remote=bool(getattr(request, "fetch_images", False)),
                cfg=self.config, vision_llm=llm,
                cache_dir=root / "analysis" / ".image-cache",
                reporter=reporter,
                # stub 模式不做视觉调用（会打网络且无意义），但仍产出图片清单产物
                analyze=not getattr(request, "stub", False),
            )
        except Exception as exc:
            warnings.warn(f"图片分析失败（不影响生成）: {type(exc).__name__}: {exc}")
            return None
        if spec.refs:
            try:
                atomic_write_text(root / "analysis" / "image-spec.json",
                                  json.dumps(images.to_json(spec), ensure_ascii=False,
                                             indent=2) + "\n")
            except Exception as exc:
                warnings.warn(f"图片证据留痕失败（不影响生成）: {exc}")
        return spec

    def cancel(self) -> None:
        """请求取消：流式回调逐块检查该标志，命中即停止接收响应以止损。"""
        self.cancel_flag.cancel()
        self.live.mark_cancelled()

    def generate(self, request: GenerateRequest, progress_callback: Callable | None = None,
                 event_callback: Callable | None = None) -> RunResult:
        from prd2tc import loader
        from prd2tc.stub import stub_chat
        if request.jobs < 1: raise ValueError("jobs 必须大于等于 1")
        profile = get_profile(request.profile)
        min_cases = request.min_cases_per_requirement or profile["minCasesPerRequirement"]
        # PRD 摄取：统一走 ingest（支持 md/txt/docx/pdf/zip/图片/多文件）。
        # 服务层上传后可直接传 bundle，避免重复读盘与重复解析。
        bundle = request.bundle if request.bundle is not None else loader.load_bundle(
            request.prd, extra_dirs=getattr(request, "image_dirs", None))
        text = bundle.text
        path = _prd_path(request, bundle)
        if not text.strip() and not bundle.images():
            raise ValueError("PRD 无可用内容（既没有文本，也没有可识别的图片）")
        product = request.product or path.parent.name; version = request.version or extract_version(text, path)
        root = Path(request.out) / product / version
        # **必须在 init_output 之前采样**：init_output 会重写清单，
        # 抹掉「已有产物属于哪份 PRD」的记录。若在其后才读，
        # 陈旧检测拿到的永远是刚写入的新哈希，等于失效（实测踩到）。
        recorded_prd_hash = artifacts.read_manifest_hash(root)
        if not request.stub and not request.dry_run and self.config.requires_api_key() and not self.config.api_key: raise ValueError("未配置 LLM 凭证")
        if not request.resume or request.force or (not (root / MANIFEST_NAME).exists() and not (root / LEGACY_MANIFEST_NAME).exists()):
            init_output(root, product, version, str(path.resolve()))
        # 两个哈希各司其职（不可混用）：
        #   prdHash    = 正文文本哈希。**已发布契约** —— `cmd_diff` 用它判断
        #                「新 PRD 是否与历史运行同一份」，掺入资产指纹会让它永不匹配，
        #                导致 diff 把未改动的 PRD 误判为 modified（实测踩到）。
        #   inputHash  = 正文 + 资产（含图片内容）。`--resume` 用它，
        #                这样换了附件/图片就会使旧检查点失效。
        prd_hash = hashlib.sha256(text.encode()).hexdigest()
        bundle_hash = bundle.fingerprint()
        input_hash = hashlib.sha256(f"{prd_hash}\x00{bundle_hash}".encode()).hexdigest()
        # llm 提前确定：图片视觉分析需要它，而图片结论要参与随后的 configHash
        # （它只依赖 self.llm / self._custom_llm / request.stub，无副作用，可安全前移）
        llm = self.llm if self._custom_llm or not request.stub else stub_chat
        # 原型/交互式设计产物 → UI 契约（真值）。内容纳入 configHash：
        # 改了原型就必须重新生成，不能复用按旧标识符产出的检查点。
        ui_spec, attachments_meta = self._load_attachments(request.attachments, root)
        if ui_spec is None:
            # 未显式 --attach 时，尝试从上传的文件里自动识别原型（UI 上传文件夹的常见形态）
            ui_spec, auto_meta = self._auto_prototypes(bundle, root)
            attachments_meta = list(attachments_meta) + list(auto_meta)
        # PRD 图片 → 视觉证据（来自 bundle.assets：上传/压缩包/DOCX 内嵌/目录）
        image_spec = None
        if request.analyze_images:
            image_spec = self._load_images(
                bundle, root, request=request, llm=llm, reporter=lambda *_: None)
        # 摄取留痕：让「正文与资产是从哪些文件来的」可追溯（含告警与内容指纹）
        if bundle.sources:
            try:
                atomic_write_text(root / "analysis" / "prd-bundle.json",
                                  json.dumps(ingest.to_json(bundle), ensure_ascii=False,
                                             indent=2) + "\n")
            except Exception as exc:
                warnings.warn(f"摄取留痕失败（不影响生成）: {exc}")
        examples = request.examples or []
        example_hashes = []
        examples_meta = []
        for example in examples:
            raw = json.dumps({"module": example.module, "title": example.title,
                              "requirement": example.requirement, "designLogic": example.design_logic,
                              "cases": example.cases}, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            example_hashes.append(digest)
            examples_meta.append({"module": example.module, "title": example.title, "sha256": digest})
        prompt_meta = {}
        prompt_hashes = []
        for name in (stages.PROMPT_1, stages.PROMPT_2, stages.PROMPT_3,
                     "prompts/00-testing-principles.md", "prompts/04-case-constraints.md"):
            value = loader.load_asset(name)
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            match = re.search(r"prompt v[\d.]+", value)
            prompt_meta[name] = {"version": match.group(0) if match else "", "sha256": digest}
            prompt_hashes.append(digest)
        prompt_hash = hashlib.sha256("|".join(prompt_hashes + example_hashes).encode("utf-8")).hexdigest()
        config_hash = hashlib.sha256(json.dumps({"provider": self.config.provider, "model": self.config.model,
            "temperature": self.config.temperature, "maxGateRetries": request.max_gate_retries,
            "promptHash": prompt_hash, "profile": request.profile, "minCases": min_cases,
            "repro": getattr(self.config, "repro", False), "seed": getattr(self.config, "seed", None),
            # 摄取结果（正文 + 资产内容）变更必须使旧检查点失效
            "bundleHash": bundle_hash,
            # 原型契约决定所有 target 与文案的真值：变更必须使旧检查点失效
            "uiSpecHash": hashlib.sha256(json.dumps(
                prototype.to_json(ui_spec), ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest() if ui_spec is not None else None,
            # 图片视觉结论直接影响需求与用例：变更必须使旧检查点失效
            "imageSpecHash": hashlib.sha256(json.dumps(
                images.to_json(image_spec), ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest() if image_spec is not None else None,
            # maxTokens 决定单次输出是否会被截断，会实质影响产物完整性：
            # 纳入 configHash，确保改变输出上限后不会复用「在上限更小时生成的」检查点。
            "maxTokens": self.config.max_tokens},
            sort_keys=True).encode("utf-8")).hexdigest()
        state_path = root / "run-state.json"
        old_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        manifest_path = root / MANIFEST_NAME if (root / MANIFEST_NAME).exists() else root / LEGACY_MANIFEST_NAME
        old_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        if request.resume and not request.force and old_manifest.get("prdHash") and old_manifest["prdHash"] != prd_hash:
            raise ValueError("--resume 输入 PRD 与历史运行不一致；如确认覆盖请使用 --force")
        can_resume = bool(request.resume and not request.force and old_state.get("inputHash") == input_hash
                          and old_state.get("promptHash") == prompt_hash
                          and old_state.get("model", self.config.model) == self.config.model
                          and old_state.get("configHash") == config_hash)
        run_id = old_manifest.get("runId") if can_resume and old_manifest.get("runId") else str(uuid.uuid4())
        update_manifest(root, runId=run_id, prdHash=prd_hash, bundleHash=bundle_hash,
                        model=self.config.model,
                        prompts=prompt_meta, examples=examples_meta, attachments=attachments_meta,
                        parameters={"provider": self.config.provider, "apiBase": self.config.api_base,
                                    "temperature": self.config.temperature, "timeout": self.config.timeout,
                                    "maxRetries": self.config.max_retries, "jobs": request.jobs,
                                    "maxTokens": self.config.max_tokens,
                                    "profile": request.profile, "minCasesPerRequirement": min_cases,
                                    "repro": getattr(self.config, "repro", False),
                                    "seed": getattr(self.config, "seed", None)},
                        status="running")
        update_run_state(root, runId=run_id, inputHash=input_hash, promptHash=prompt_hash,
                         configHash=config_hash, model=self.config.model, status="running", stage="init",
                         modules=old_state.get("modules", {}) if can_resume else {})
        result = RunResult(run_id, root)
        notify = progress_callback or (lambda *_: None)
        # 实时指标：内存态，不落盘事件（事件写入是整文件重写，逐 token 会 O(n²)）
        live = self.live
        result.metrics = live
        cancel = self.cancel_flag
        metrics.bind(live, "")
        metrics.bind_cancel(cancel)

        def emit(kind: str, **fields) -> None:
            """P0 结构化进度事件：走独立的 event_callback。

            刻意不复用 progress_callback —— 那是「阶段 33/66/100」的旧契约，
            CLI 与外部调用方按 (stage, percent) 打印，混入模块/批次/心跳会污染输出。
            """
            if event_callback is None:
                return
            payload = {"type": kind, **fields}
            try:
                event_callback(payload)
            except Exception:
                pass

        def on_batch(module: str, index: int, total: int, phase: str) -> None:
            live.set_batch(module, index, total)
            if phase == "module_started":
                live.begin_module(module, batches=total)
            emit(phase, module=module, batch=f"{index}/{total}", percent=0, modules=len(getattr(live, "_modules", {})))

        if request.dry_run:
            result.prompts = {"analysis": stages.build_stage_1_user(text), "plan": stages.build_stage_2_user("…"), "cases": stages.build_stage_3_user("…")}
            update_manifest(root, status="succeeded"); update_run_state(root, status="succeeded", stage="done"); return result
        try:
            analysis_path = root / "analysis" / "需求分析报告.md"; req_path = root / "analysis" / "requirements.json"
            # 本次真正要执行的阶段（`--stage N` 只跑 N；`--from-stage N` 跑 N..3；`all` 全跑）
            running_stages = set(artifacts.stages_to_run(
                request.stage, getattr(request, "from_stage", "") or ""))
            # 链路完备性 preflight：对**将要执行**的每个阶段，校验其输入产物完备；
            # 但若某输入由**同样要执行**的上游阶段产出，则跳过 —— 那是本次会生成的东西。
            #
            # 例：`--stage 3`（只跑用例）→ 必须先有 plan 与 requirements；
            #     `--stage all`      → 阶段 1 的产物稍后自会生成，无需预检。
            # 替代原先散落的 `.exists()` —— 后者会放过空文件、`requirements: []`、
            # 以及「用另一份 PRD 生成的旧产物」这三类静默错配（见 artifacts.py）。
            for stage_key in running_stages:
                for artifact in artifacts.STAGE_INPUTS.get(stage_key, ()):
                    if artifact.stage in running_stages:
                        continue          # 该产物由本次运行的上游阶段产出
                    problems = artifacts.check_one(
                        root, artifact, prd_hash=prd_hash,
                        recorded_hash=recorded_prd_hash,
                        allow_stale=bool(getattr(request, "allow_stale_artifacts", False)))
                    if problems:
                        raise ValueError(artifacts.format_problems(problems, stage_key))
            # `--resume` 时若三阶段全在计划内且产物齐全，则整段复用（跳过重跑）
            resume_all = (can_resume and running_stages == set(artifacts.STAGE_ORDER)
                          and analysis_path.exists() and req_path.exists())
            if "1" in running_stages and not resume_all:
                live.set_stage("analysis"); metrics.bind(live, "analysis")
                emit("stage_started", stage="analysis", percent=0)
                started = time.perf_counter(); analysis, r1, errors = stages.generate_stage_1(self.config, text, llm=llm, examples=request.examples, jobs=request.jobs, use_gate=not (request.stub or request.no_gate), max_gate_retries=request.max_gate_retries, reporter=lambda *_: None, image_context=images.render_image_spec(image_spec) if image_spec else "")
                reqs = stages.requirements_from_analysis(analysis, text); analysis = stages.align_analysis_ids(analysis, reqs)
                write_stage(root, "analysis", analysis); save_requirements(req_path, reqs, stages.modules_from_analysis(analysis, reqs))
                manifest_stage(root, "analysis", model=r1.model, tokens=r1.total_tokens, durationMs=round((time.perf_counter()-started)*1000,1), retries=r1.gate_retries, status="failed" if errors else "succeeded", gate={"errors": errors})
                live.finish_module("analysis", "failed" if errors else "done")
                notify("analysis", 33)
                if errors and not request.allow_gate_fail: return self._fail(result, errors)
            else:
                if not analysis_path.exists(): raise FileNotFoundError(f"缺少阶段1产物: {analysis_path}")
                analysis = analysis_path.read_text(encoding="utf-8"); reqs = load_requirements(req_path)
            plan_path = root / "plan" / "测试方案.md"
            if "2" in running_stages and not (can_resume and "3" in running_stages and plan_path.exists()):
                live.set_stage("plan"); metrics.bind(live, "plan")
                emit("stage_started", stage="plan", percent=33)
                started = time.perf_counter()
                # 阶段 2 用紧凑分析报告（需求点明细大表替换为一行一需求），减少 token
                analysis_compact = stages.compact_analysis(analysis, reqs)
                if request.stub or request.no_gate:
                    plan, r2 = stages.run_stage_2(self.config, analysis_compact, llm=llm)
                    errors = []
                else:
                    from prd2tc import gates
                    plan, r2, errors = stages.generate_with_feedback(
                        self.config, lambda feedback: stages.build_stage_2_user(analysis_compact, feedback=feedback),
                        gates.check_plan, llm=llm, max_retries=request.max_gate_retries,
                        label="阶段2 测试方案", system=stages.SYSTEM_ROLES[2],
                        temperature=stages.stage_temperature(2, self.config), reporter=lambda *_: None)
                write_stage(root, "plan", plan)
                manifest_stage(root, "plan", model=r2.model, tokens=r2.total_tokens,
                               durationMs=round((time.perf_counter()-started)*1000, 1),
                               retries=r2.gate_retries, status="failed" if errors else "succeeded",
                               gate={"errors": errors})
                live.finish_module("plan", "failed" if errors else "done")
                notify("plan", 66)
                if errors and not request.allow_gate_fail:
                    return self._fail(result, errors)
            else:
                # 阶段 2 被跳过：若后续还要跑阶段 3，方案文件必须存在
                # （完备性已在 preflight 校验，这里只需读取）
                if "3" in running_stages and not plan_path.exists():
                    raise FileNotFoundError(f"缺少阶段2产物: {plan_path}")
                plan = plan_path.read_text(encoding="utf-8") if plan_path.exists() else ""
            if "3" in running_stages:
                live.set_stage("cases")
                emit("stage_started", stage="cases", percent=66)
                if ui_spec is not None:
                    emit("ui_spec_loaded", stage="cases", percent=66,
                         attachments=[m["name"] for m in attachments_meta],
                         anchors=len(ui_spec.anchor_values()),
                         pages=[p["key"] for p in ui_spec.pages],
                         messages=len(ui_spec.messages))
                started=time.perf_counter(); cdir=root/"cases"/".checkpoints"; lock=threading.Lock(); modules=dict(old_state.get("modules",{})) if can_resume else {}
                def status(m,s):
                    with lock: modules[m]=s; update_run_state(root,stage="cases",status="running",modules=dict(modules))
                    if s in {"failed"}:
                        live.finish_module(m, "failed")
                    elif s in {"skipped"}:
                        live.finish_module(m, "skipped")
                    emit("module_finished" if s in {"failed","skipped"} else "module_progress",
                         stage="cases", module=m, percent=66, state=s)
                def load(m):
                    p=cdir/f"{m}.json"; return normalize_cases(json.loads(p.read_text(encoding="utf-8"))) if can_resume and modules.get(m) in {"succeeded","skipped"} and p.exists() else None
                def save(m,c):
                    atomic_write_text(cdir/f"{m}.json",json.dumps(c,ensure_ascii=False,indent=2)+"\n"); status(m,"succeeded")
                def fail_save(m,text):
                    fdir=root/"cases"/".failures"; fdir.mkdir(parents=True,exist_ok=True)
                    atomic_write_text(fdir/f"{m}.txt",text)
                module_tokens = {}
                def tok_cb(m, p, c, cr=0, cc=0):
                    with lock:  # 此前为无锁写入；jobs>1 时与 status 回调并发
                        module_tokens[m] = {"promptTokens": p, "completionTokens": c,
                                            "cacheReadTokens": cr, "cacheCreationTokens": cc,
                                            "totalTokens": p + c}
                    live.finish_module(m, "done")
                    emit("module_finished", stage="cases", module=m, percent=66, state="succeeded",
                         tokens=p + c)
                try: cases,r3=stages.run_stage_3_modules(self.config,plan,reqs,text,llm=llm,examples=request.examples,jobs=request.jobs,use_gate=not(request.stub or request.no_gate),max_gate_retries=request.max_gate_retries,load_checkpoint=load,save_checkpoint=save,save_failure=fail_save,token_callback=tok_cb,status_callback=status,batch_callback=on_batch,reporter=lambda *_: None,ui_spec=ui_spec,image_context=images.render_image_spec(image_spec) if image_spec else "")
                except ValidationError as e: cases=[]; r3=LLMResult("",self.config.model); errors=list(e.errors)
                else: errors=[]
                # 优先级确定性重判：与 id 重编号、模块码强制同构。
                # 必须在 build_traceability 与落盘**之前**，且同步维护 @smoke
                # （门禁要求 P0 必带，而重判会产生新的 P0）。
                if cases:
                    priority.regrade(cases, reqs)
                matrix=build_traceability(reqs,cases,min_cases=min_cases) if not errors else {}; errors += validate_traceability(matrix) if matrix else []
                if errors and not request.allow_gate_fail:
                    # 门禁失败时**不再丢弃**已生成的用例：实测运行 9d97060e 已产出 43 条，
                    # 却因最后一步覆盖率不过而全部消失，用户只看到「失败」与空目录。
                    # 写入 cases/partial/（独立目录 + 显著标记），既保留可修复的工作成果，
                    # 又不会被误当成正式产物。正式 cases/cases/ 保持为空。
                    if cases:
                        save_cases(root/"cases"/"partial", cases)
                        atomic_write_text(
                            root/"cases"/"partial"/"READ-ME-部分用例未通过门禁.md",
                            "# ⚠ 部分用例（未通过门禁，请勿直接采信）\n\n"
                            f"本次运行的门禁未通过，以下 {len(cases)} 条用例是**已生成但覆盖不完整**的结果。\n\n"
                            "## 门禁错误\n\n"
                            + "\n".join(f"- {e}" for e in errors) + "\n\n"
                            "## 处置建议\n\n"
                            "1. 先判断根因类型：模型输出不完整 / PRD 事实缺失 / 数据契约错误 / Provider 连接问题；\n"
                            "2. 若为个别模块输出不完整，重试失败模块即可（run-state.json 的 modules 会标出 failed）；\n"
                            "3. 若为 PRD 事实缺失，编辑 PRD 后重新分析；\n"
                            "4. **不要把本目录的结果作为正式交付**。\n")
                    manifest_stage(root,"cases",model=r3.model,tokens=r3.total_tokens,durationMs=round((time.perf_counter()-started)*1000,1),status="failed",gate={"errors":errors},moduleTokens=module_tokens,partialCaseCount=len(cases)); return self._fail(result,errors)
                save_cases(root/"cases"/"cases",cases); save_traceability(root/"cases"/"traceability.json",matrix); write_stage(root,"cases",render_cases_md(cases,plan_title="PRD v"+version));
                # Excel / XMind 导出：**必须**局部 try/except —— 外层 except BaseException 会把
                # 导出失败升级为整条运行失败，违背「导出失败不阻断主流程」（F-8）。
                try:
                    from prd2tc.export import export_cases
                    export_info = export_cases(cases_dir=root/"cases"/"cases", formats=["excel","xmind"],
                                               root=root, include_traceability=True,
                                               product=product, version=version)
                    exported = [p.name for p in export_info.get("files", [])]
                except Exception as exc:
                    exported = []
                    warnings.warn(f"导出 Excel/XMind 失败（不影响生成结果）: {type(exc).__name__}: {exc}")
                manifest_stage(root,"cases",model=r3.model,tokens=r3.total_tokens,durationMs=round((time.perf_counter()-started)*1000,1),status="succeeded" if not errors else "failed",gate={"errors":errors},moduleTokens=module_tokens,exports=exported); result.cases=cases; result.requirements=reqs; result.statistics=stats(cases); notify("cases",100)
            result.requirements=reqs; result.status="succeeded"; update_manifest(root,artifacts=artifact_inventory(root),status="succeeded"); update_run_state(root,status="succeeded",stage="done")
            self.live.final_snapshot()
            try:  # 结构指纹（可复现性契约，排除自由文本）
                from prd2tc.reproduce import run_fingerprint
                atomic_write_text(root/"reproduce.json", json.dumps(run_fingerprint(root),ensure_ascii=False,indent=2)+"\n")
            except Exception: pass
            return result
        except BaseException as exc:
            detail = format_exception(exc)
            update_manifest(root, status="failed", error=detail)
            update_run_state(root, status="failed", stage="failed")
            self.live.final_snapshot()
            result.status = "failed"
            result.errors = list(getattr(exc, "errors", None) or [detail])
            raise

    @staticmethod
    def _fail(result: RunResult, errors: list[str]) -> RunResult:
        result.status="failed"; result.errors=list(errors); update_manifest(result.root,status="failed",artifacts=artifact_inventory(result.root)); update_run_state(result.root,status="failed",stage="done")
        metrics = getattr(result, "metrics", None)
        if metrics is not None:
            metrics.fail_active("；".join(errors[:3]))  # 附上门禁原因，UI 才能直接定位
            metrics.final_snapshot()   # 失败也要收敛指标，否则 UI 一直显示「进行中」
        return result
