"""统一生成编排内核，供 CLI、Python API 和服务层复用。"""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib, json, re, threading, time, uuid
from pathlib import Path
from typing import Callable

from prd2tc import stages
from prd2tc.config import Config
from prd2tc.io import (MANIFEST_NAME, LEGACY_MANIFEST_NAME, artifact_inventory,
    atomic_write_text, init_output, load_requirements, manifest_stage, render_cases_md,
    save_cases, save_requirements, save_traceability, stats, update_manifest,
    update_run_state, write_stage)
from prd2tc.llm import LLMResult
from prd2tc.profiles import get_profile
from prd2tc.schema import normalize_cases
from prd2tc.traceability import build_traceability, validate_traceability
from prd2tc.validator import ValidationError

@dataclass
class GenerateRequest:
    prd: str | Path
    out: str | Path = "output"
    product: str | None = None
    version: str | None = None
    examples: list | None = None
    stub: bool = False
    dry_run: bool = False
    jobs: int = 1
    stage: str = "all"
    profile: str = "generic"
    min_cases_per_requirement: int | None = None
    max_gate_retries: int = 2
    allow_gate_fail: bool = False
    no_gate: bool = False
    resume: bool = False
    force: bool = False

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

def extract_version(text: str, path: Path) -> str:
    m = re.search(r"版本[：:]\s*(v[\d.]+)", text)
    if m: return m.group(1)
    m = re.search(r"PRD-[^-]+-(v[\d.]+)\.md$", path.name)
    return m.group(1) if m else "v0.1"

class PipelineRunner:
    def __init__(self, config: Config, *, llm=None):
        self.config = config
        self._custom_llm = llm is not None
        self.llm = llm or stages.chat_completion

    def generate(self, request: GenerateRequest, progress_callback: Callable | None = None) -> RunResult:
        from prd2tc import loader
        from prd2tc.stub import stub_chat
        if request.jobs < 1: raise ValueError("jobs 必须大于等于 1")
        profile = get_profile(request.profile)
        min_cases = request.min_cases_per_requirement or profile["minCasesPerRequirement"]
        text = loader.load_prd(request.prd); path = Path(request.prd)
        product = request.product or path.parent.name; version = request.version or extract_version(text, path)
        root = Path(request.out) / product / version
        if not request.stub and not request.dry_run and self.config.requires_api_key() and not self.config.api_key: raise ValueError("未配置 LLM 凭证")
        if not request.resume or request.force or (not (root / MANIFEST_NAME).exists() and not (root / LEGACY_MANIFEST_NAME).exists()):
            init_output(root, product, version, str(path.resolve()))
        prd_hash = hashlib.sha256(text.encode()).hexdigest()
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
            "repro": getattr(self.config, "repro", False), "seed": getattr(self.config, "seed", None)},
            sort_keys=True).encode("utf-8")).hexdigest()
        state_path = root / "run-state.json"
        old_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        manifest_path = root / MANIFEST_NAME if (root / MANIFEST_NAME).exists() else root / LEGACY_MANIFEST_NAME
        old_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        if request.resume and not request.force and old_manifest.get("prdHash") and old_manifest["prdHash"] != prd_hash:
            raise ValueError("--resume 输入 PRD 与历史运行不一致；如确认覆盖请使用 --force")
        can_resume = bool(request.resume and not request.force and old_state.get("inputHash") == prd_hash
                          and old_state.get("promptHash") == prompt_hash
                          and old_state.get("model", self.config.model) == self.config.model
                          and old_state.get("configHash") == config_hash)
        run_id = old_manifest.get("runId") if can_resume and old_manifest.get("runId") else str(uuid.uuid4())
        update_manifest(root, runId=run_id, prdHash=prd_hash, model=self.config.model,
                        prompts=prompt_meta, examples=examples_meta,
                        parameters={"provider": self.config.provider, "apiBase": self.config.api_base,
                                    "temperature": self.config.temperature, "timeout": self.config.timeout,
                                    "maxRetries": self.config.max_retries, "jobs": request.jobs,
                                    "profile": request.profile, "minCasesPerRequirement": min_cases,
                                    "repro": getattr(self.config, "repro", False),
                                    "seed": getattr(self.config, "seed", None)},
                        status="running")
        update_run_state(root, runId=run_id, inputHash=prd_hash, promptHash=prompt_hash,
                         configHash=config_hash, model=self.config.model, status="running", stage="init",
                         modules=old_state.get("modules", {}) if can_resume else {})
        result = RunResult(run_id, root)
        notify = progress_callback or (lambda *_: None)
        llm = self.llm if self._custom_llm or not request.stub else stub_chat
        if request.dry_run:
            result.prompts = {"analysis": stages.build_stage_1_user(text), "plan": stages.build_stage_2_user("…"), "cases": stages.build_stage_3_user("…")}
            update_manifest(root, status="succeeded"); update_run_state(root, status="succeeded", stage="done"); return result
        try:
            analysis_path = root / "analysis" / "需求分析报告.md"; req_path = root / "analysis" / "requirements.json"
            if request.stage in {"1", "all"} and not (can_resume and request.stage == "all" and analysis_path.exists() and req_path.exists()):
                started = time.perf_counter(); analysis, r1, errors = stages.generate_stage_1(self.config, text, llm=llm, examples=request.examples, jobs=request.jobs, use_gate=not (request.stub or request.no_gate), max_gate_retries=request.max_gate_retries, reporter=lambda *_: None)
                reqs = stages.requirements_from_analysis(analysis, text); analysis = stages.align_analysis_ids(analysis, reqs)
                write_stage(root, "analysis", analysis); save_requirements(req_path, reqs, stages.modules_from_analysis(analysis, reqs))
                manifest_stage(root, "analysis", model=r1.model, tokens=r1.total_tokens, durationMs=round((time.perf_counter()-started)*1000,1), retries=r1.gate_retries, status="failed" if errors else "succeeded", gate={"errors": errors})
                notify("analysis", 33)
                if errors and not request.allow_gate_fail: return self._fail(result, errors)
            else:
                if not analysis_path.exists(): raise FileNotFoundError(f"缺少阶段1产物: {analysis_path}")
                analysis = analysis_path.read_text(encoding="utf-8"); reqs = load_requirements(req_path)
            plan_path = root / "plan" / "测试方案.md"
            if request.stage in {"2", "all"} and not (can_resume and request.stage == "all" and plan_path.exists()):
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
                notify("plan", 66)
                if errors and not request.allow_gate_fail:
                    return self._fail(result, errors)
            else:
                if request.stage == "3" and not plan_path.exists(): raise FileNotFoundError(f"缺少阶段2产物: {plan_path}")
                plan = plan_path.read_text(encoding="utf-8") if plan_path.exists() else ""
            if request.stage in {"3", "all"}:
                started=time.perf_counter(); cdir=root/"cases"/".checkpoints"; lock=threading.Lock(); modules=dict(old_state.get("modules",{})) if can_resume else {}
                def status(m,s):
                    with lock: modules[m]=s; update_run_state(root,stage="cases",status="running",modules=dict(modules))
                def load(m):
                    p=cdir/f"{m}.json"; return normalize_cases(json.loads(p.read_text(encoding="utf-8"))) if can_resume and modules.get(m) in {"succeeded","skipped"} and p.exists() else None
                def save(m,c):
                    atomic_write_text(cdir/f"{m}.json",json.dumps(c,ensure_ascii=False,indent=2)+"\n"); status(m,"succeeded")
                def fail_save(m,text):
                    fdir=root/"cases"/".failures"; fdir.mkdir(parents=True,exist_ok=True)
                    atomic_write_text(fdir/f"{m}.txt",text)
                module_tokens = {}
                def tok_cb(m, p, c, cr=0, cc=0):
                    module_tokens[m] = {"promptTokens": p, "completionTokens": c,
                                        "cacheReadTokens": cr, "cacheCreationTokens": cc,
                                        "totalTokens": p + c}
                try: cases,r3=stages.run_stage_3_modules(self.config,plan,reqs,text,llm=llm,examples=request.examples,jobs=request.jobs,use_gate=not(request.stub or request.no_gate),max_gate_retries=request.max_gate_retries,load_checkpoint=load,save_checkpoint=save,save_failure=fail_save,token_callback=tok_cb,status_callback=status,reporter=lambda *_: None)
                except ValidationError as e: cases=[]; r3=LLMResult("",self.config.model); errors=list(e.errors)
                else: errors=[]
                matrix=build_traceability(reqs,cases,min_cases=min_cases) if not errors else {}; errors += validate_traceability(matrix) if matrix else []
                if errors and not request.allow_gate_fail: manifest_stage(root,"cases",model=r3.model,tokens=r3.total_tokens,durationMs=round((time.perf_counter()-started)*1000,1),status="failed",gate={"errors":errors},moduleTokens=module_tokens); return self._fail(result,errors)
                save_cases(root/"cases"/"cases",cases); save_traceability(root/"cases"/"traceability.json",matrix); write_stage(root,"cases",render_cases_md(cases,plan_title="PRD v"+version)); manifest_stage(root,"cases",model=r3.model,tokens=r3.total_tokens,durationMs=round((time.perf_counter()-started)*1000,1),status="succeeded" if not errors else "failed",gate={"errors":errors},moduleTokens=module_tokens); result.cases=cases; result.requirements=reqs; result.statistics=stats(cases); notify("cases",100)
            result.requirements=reqs; result.status="succeeded"; update_manifest(root,artifacts=artifact_inventory(root),status="succeeded"); update_run_state(root,status="succeeded",stage="done")
            try:  # 结构指纹（可复现性契约，排除自由文本）
                from prd2tc.reproduce import run_fingerprint
                atomic_write_text(root/"reproduce.json", json.dumps(run_fingerprint(root),ensure_ascii=False,indent=2)+"\n")
            except Exception: pass
            return result
        except BaseException as exc:
            update_manifest(root,status="failed",error=f"{type(exc).__name__}: {exc}"); update_run_state(root,status="failed",stage="failed"); raise

    @staticmethod
    def _fail(result: RunResult, errors: list[str]) -> RunResult:
        result.status="failed"; result.errors=list(errors); update_manifest(result.root,status="failed",artifacts=artifact_inventory(result.root)); update_run_state(result.root,status="failed",stage="done"); return result
