"""prd2tc 命令行入口；生成流程统一委托给 PipelineRunner。"""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys
from dataclasses import replace
from pathlib import Path
from prd2tc import __version__, loader, stages
from prd2tc.config import Config
from prd2tc.diff import diff_requirements, merge_incremental_cases, requirements_from_prd
from prd2tc.execution import apply_execution_results, load_execution_result
from prd2tc.io import (MANIFEST_NAME, LEGACY_MANIFEST_NAME, atomic_write_text,
                       artifact_inventory, init_output, load_cases, save_cases, stats,
                       update_manifest)
from prd2tc.pipeline import GenerateRequest, PipelineRunner, extract_version
from prd2tc.profiles import get_profile
from prd2tc.validator import ValidationError, validate_dir
S = stages  # 兼容既有调用方；生成编排已迁移至 PipelineRunner。

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def _stub_chat(cfg, user_text: str, **kwargs):
    from prd2tc.stub import stub_chat
    return stub_chat(cfg, user_text, **kwargs)

def _load_examples(args, prd_text: str) -> list:
    from prd2tc.examples import load_examples, retrieve
    directory = args.examples or os.environ.get("PRD2TC_EXAMPLES")
    if not directory: return []
    selected = retrieve(load_examples(directory), prd_text, k=args.examples_k)
    if selected: print(f"📚 已注入 {len(selected)} 条参考示例: {[e.module for e in selected]}")
    return selected

def cmd_generate(args: argparse.Namespace) -> int:
    cfg = Config.from_env(args.provider)
    if args.api_base: cfg = replace(cfg, api_base=args.api_base.rstrip("/"))
    if args.model: cfg = replace(cfg, model=args.model)
    if args.repro: cfg = replace(cfg, repro=True)
    if args.seed is not None: cfg = replace(cfg, seed=args.seed)
    text = loader.load_prd(args.prd); examples = [] if args.dry_run else _load_examples(args, text)
    profile = get_profile(args.profile); minimum = args.min_cases_per_requirement or profile["minCasesPerRequirement"]
    if not args.stub and not args.dry_run and cfg.requires_api_key() and not cfg.api_key:
        print("✗ 未配置 LLM 凭证：设置对应 Provider 的 API Key（或使用 --stub/--dry-run）"); return 2
    request = GenerateRequest(prd=args.prd, out=args.out, product=args.product,
                              version=args.ver or extract_version(text, Path(args.prd)), examples=examples,
                              stub=args.stub, dry_run=args.dry_run, jobs=args.jobs, stage=args.stage,
                              profile=args.profile, min_cases_per_requirement=minimum,
                              max_gate_retries=args.max_gate_retries, allow_gate_fail=args.allow_gate_fail,
                              no_gate=args.no_gate, resume=args.resume, force=args.force)
    def progress(stage, percent):
        print(f"✔ {stage} ({percent}%)")
        if args.review and stage in {"analysis", "plan"}: input("按 Enter 确认后继续…")
    try:
        result = PipelineRunner(cfg, llm=_stub_chat if args.stub else None).generate(request, progress_callback=progress)
    except (FileNotFoundError, ValueError, ValidationError) as exc:
        print(f"✗ {exc}"); return 1
    if args.dry_run:
        for name, prompt in result.prompts.items(): print(f"\n===== {name} prompt =====\n{prompt}")
    _print_tokens(result.root)
    if result.status != "succeeded":
        for error in result.errors[:8]: print(f"  ✗ {error}")
        return 1
    if result.statistics: print(f"用例 {result.statistics['total']} 条，覆盖 REQ {result.statistics['req_covered']} 个")
    print(f"输出目录: {result.root}\n完成。")
    return 0


def _print_tokens(root: Path) -> None:
    """打印 run-manifest 中的 token 账单（含门禁重试的累计值与 Prompt 缓存命中量）。"""
    manifest_path = root / MANIFEST_NAME if (root / MANIFEST_NAME).exists() else root / LEGACY_MANIFEST_NAME
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stages_meta = manifest.get("stages") or {}
    if not stages_meta:
        return
    total = 0
    print("\nToken 消耗：")
    for name, meta in stages_meta.items():
        tokens = meta.get("tokens", 0)
        total += tokens
        print(f"  {name}: {tokens} tokens（重试 {meta.get('retries', 0)} 次，耗时 {meta.get('durationMs', 0)}ms）")
        for module, mt in sorted((meta.get("moduleTokens") or {}).items()):
            t = mt.get("totalTokens", mt.get("promptTokens", 0) + mt.get("completionTokens", 0))
            line = f"    - {module}: {t} tokens"
            if mt.get("cacheReadTokens"):
                line += f"（缓存命中 {mt['cacheReadTokens']}，写入缓存 {mt.get('cacheCreationTokens', 0)}）"
            print(line)
    print(f"  合计: {total} tokens")

def cmd_probe_cache(args):
    """探测 Provider 的 Prompt 缓存：对相同 system 前缀连发两次请求，观察 usage 缓存字段是否回显。

    - cache_creation_input_tokens > 0  → 首次调用写入缓存（显式断点生效）
    - cache_read_input_tokens > 0（第 2 次）→ 命中缓存
    兼容端点（如 MiniMax M3）即使不回显也不报错，是否缓存以本命令实际回显为准。
    """
    cfg = Config.from_env(args.provider or "anthropic")
    if args.api_base: cfg = replace(cfg, api_base=args.api_base.rstrip("/"))
    if args.model: cfg = replace(cfg, model=args.model)
    if cfg.requires_api_key() and not cfg.api_key:
        print("✗ 未配置 API Key（PRD2TC_API_KEY / ANTHROPIC_API_KEY）"); return 2
    print(f"缓存探测：provider={cfg.provider} model={cfg.model} base={cfg.api_base}")
    print(f"cache_control：{'开启（PRD2TC_PROMPT_CACHE=1）' if cfg.prompt_cache else '关闭（设 PRD2TC_PROMPT_CACHE=1 可加显式断点）'}")
    # 模拟阶段3 的稳定 system 前缀体量（约 2K+ token），低于缓存阈值则探测无意义
    system = "你是一位资深测试架构师。\n\n" + ("· 稳定的测试方案与用例约束前缀内容（用于验证 Prompt 缓存是否生效）。\n" * 320)
    from prd2tc.llm import create_client
    client = create_client(cfg)
    results = []
    for i in (1, 2):
        result = client.chat("只回复两个字：收到", system=system, temperature=0.2)
        results.append(result)
        print(f"  第{i}次  input={result.prompt_tokens:>6} output={result.completion_tokens:>5} "
              f"cache_read={result.cache_read_input_tokens:>6} cache_creation={result.cache_creation_input_tokens:>6}  {result.model}")
    r1, r2 = results
    if r2.cache_read_input_tokens > 0:
        print("✓ 第 2 次请求命中缓存（cache_read_input_tokens > 0）→ 相同 system 前缀会被缓存复用")
    elif r1.cache_creation_input_tokens > 0 and r2.cache_read_input_tokens == 0:
        print("⚠ 首次写入了缓存但第 2 次未命中：可能 TTL 过短 / 前缀在首尾有变化 / 端点按需逐请求判定")
    else:
        print("✗ 两端缓存字段均无回显（或为 0）：该端点未启用/不支持 Prompt 缓存（或缓存阈值高于本探测前缀）")
        print("  提示：若你在真实 run 里看到每批 input_tokens 全额计费，则缓存未生效，需从输入瘦身（P0/P3 已做）继续压成本")
    return 0

def cmd_init(args):
    root = Path(args.out) / args.product / args.ver; init_output(root, args.product, args.ver, args.prd); print(f"✔ 已初始化: {root}"); return 0

def cmd_prompts(args):
    for path in sorted(loader.REPO_PROMPTS.glob("*.md")):
        text = path.read_text(encoding="utf-8"); match = re.search(r"prompt v[\d.]+", text); digest = hashlib.sha256(text.encode()).hexdigest()[:12]
        print(f"{path.name:36s} {match.group(0) if match else '未声明版本':>12s} sha256={digest}")
    return 0

def cmd_validate(args):
    try: total, errors = validate_dir(args.dir)
    except FileNotFoundError as exc: print(f"✗ {exc}"); return 1
    if errors:
        print(f"校验失败：{len(errors)} 个问题"); [print(f"  ✗ {e}") for e in errors]; return 1
    print(f"✔ 校验通过：{total} 条用例，id 唯一，字段与枚举全部合法"); return 0

def cmd_stats(args):
    try: total, errors = validate_dir(args.dir)
    except FileNotFoundError as exc: print(f"✗ {exc}"); return 1
    if errors: print(f"✗ 目录含非法用例（{len(errors)} 个问题），请先运行 validate"); return 1
    data = stats(load_cases(args.dir)); print(f"用例总数: {data['total']}\n按模块: {json.dumps(data['by_module'], ensure_ascii=False)}\n按优先级: {json.dumps(data['by_priority'], ensure_ascii=False)}\n按标签: {json.dumps(data['by_tag'], ensure_ascii=False)}\n覆盖 REQ 数: {data['req_covered']}"); return 0

def cmd_diff(args):
    text = loader.load_prd(args.prd); previous = Path(args.from_dir); old_path = previous / "analysis" / "requirements.json"
    if not old_path.exists(): print(f"✗ 缺少历史需求文件: {old_path}"); return 1
    old = json.loads(old_path.read_text(encoding="utf-8")); old_reqs = old.get("requirements", [])
    manifest_path = previous / MANIFEST_NAME if (previous / MANIFEST_NAME).exists() else previous / LEGACY_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    new_hash = hashlib.sha256(text.encode()).hexdigest(); new_reqs = old_reqs if manifest.get("prdHash") == new_hash else requirements_from_prd(text)
    result = diff_requirements(old_reqs, new_reqs); result.update(parentRunId=manifest.get("runId"), parentVersion=manifest.get("version") or previous.name, sourcePrdHash=manifest.get("prdHash"), targetPrdHash=new_hash)
    root = Path(args.out) / (args.product or Path(args.prd).parent.name) / (args.ver or extract_version(text, Path(args.prd))); out = root / "diff"; out.mkdir(parents=True, exist_ok=True)
    if (root / MANIFEST_NAME).exists() or (root / LEGACY_MANIFEST_NAME).exists():
        update_manifest(root, parentRunId=result.get("parentRunId"), parentVersion=result.get("parentVersion"))
    atomic_write_text(out / "requirement-diff.json", json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    lines = ["# 需求变更影响报告", "", f"- 新 PRD：{Path(args.prd).resolve()}", "", "## 分类", ""] + [f"- {k}: {len(result[k])}" for k in ("added","modified","removed","unchanged")]
    atomic_write_text(out / "变更影响报告.md", "\n".join(lines)+"\n"); print(f"✔ Diff 已生成: {out}"); return 0

def cmd_merge(args):
    diff_data = json.loads(Path(args.diff_file).read_text(encoding="utf-8"))
    merged = merge_incremental_cases(load_cases(Path(args.from_dir)/"cases"/"cases"), load_cases(Path(args.new_dir)/"cases"/"cases"), diff_data)
    merged["summary"].update(parentRunId=diff_data.get("parentRunId"), parentVersion=diff_data.get("parentVersion"),
                             **{"from": str(Path(args.from_dir).resolve()), "new": str(Path(args.new_dir).resolve())})
    out = Path(args.out); save_cases(out/"cases", merged["cases"]); atomic_write_text(out/"case-history.json", json.dumps(merged["history"], ensure_ascii=False, indent=2)+"\n"); atomic_write_text(out/"merge-summary.json", json.dumps(merged["summary"], ensure_ascii=False, indent=2)+"\n"); print(f"✔ 增量用例已合并: active={merged['summary']['active']} history={merged['summary']['history']}"); return 0

def cmd_import_results(args):
    updated = apply_execution_results(load_cases(args.cases), load_execution_result(args.results)); save_cases(Path(args.out), updated); print(f"✔ 已导入执行结果: {len(updated)} 条，输出 {args.out}"); return 0

def cmd_promote_examples(args):
    if not args.reviewed: print("✗ 必须显式提供 --reviewed，确认这些用例已经人工评审"); return 2
    grouped = {}
    for case in load_cases(args.cases):
        execution = case.get("lastExecution") or {}
        if execution.get("status") == "passed" and case.get("status", "active") in {"active", "reviewed"}: grouped.setdefault(str(case.get("module", "COMMON")), []).append(case)
    if not grouped: print("✗ 没有同时满足人工评审、active/reviewed、最近执行通过的用例"); return 1
    payload = [{"module": module, "title": f"{module} 已验证用例", "keywords": [module.lower()], "requirement": "、".join(sorted({r for c in cases for r in c.get("requirement", [])})), "design_logic": args.design_logic, "reviewed": True, "sourceRunIds": sorted({str((c.get("lastExecution") or {}).get("runId")) for c in cases}), "cases": cases} for module, cases in sorted(grouped.items())]
    atomic_write_text(Path(args.out), json.dumps(payload, ensure_ascii=False, indent=2)+"\n"); print(f"✔ 已导出 {len(payload)} 个模块的验证示例: {args.out}"); return 0

def cmd_compare_runs(args):
    """结构级复现对拍：同源且结构指纹一致 → 复现；同源漂移 → 归因采样并给明细。"""
    from prd2tc.reproduce import compare_runs, render_report
    try:
        report = compare_runs(args.dir_a, args.dir_b)
    except FileNotFoundError as exc:
        print(f"✗ {exc}"); return 1
    text = render_report(report)
    print(text)
    if args.out:
        atomic_write_text(Path(args.out), json.dumps(report, ensure_ascii=False, indent=2, default=str)+"\n")
        print(f"✔ 报告已写入: {args.out}")
    if args.fail_on_drift and (not report["sameSource"] or report["drift"]):
        return 1
    return 0

def build_parser():
    parser = argparse.ArgumentParser(prog="prd2tc", description="PRD → 测试方案 → 测试用例")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}"); sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate"); g.add_argument("--prd", required=True); g.add_argument("--product"); g.add_argument("--version", dest="ver"); g.add_argument("--out", default="output"); g.add_argument("--provider", choices=["openai","anthropic","ollama"]); g.add_argument("--api-base"); g.add_argument("--model"); g.add_argument("--stage", choices=["1","2","3","all"], default="all"); g.add_argument("--dry-run", action="store_true"); g.add_argument("--stub", action="store_true"); g.add_argument("--review", action="store_true"); g.add_argument("--examples"); g.add_argument("--examples-k", type=int, default=3); g.add_argument("--max-gate-retries", type=int, default=2); g.add_argument("--allow-gate-fail", action="store_true"); g.add_argument("--no-gate", action="store_true"); g.add_argument("--jobs", type=int, default=1); g.add_argument("--resume", action="store_true"); g.add_argument("--force", action="store_true"); g.add_argument("--profile", choices=["generic","ecommerce"], default="generic"); g.add_argument("--min-cases-per-requirement", type=int); g.set_defaults(func=cmd_generate)
    i = sub.add_parser("init"); i.add_argument("--product", required=True); i.add_argument("--version", required=True, dest="ver"); i.add_argument("--prd"); i.add_argument("--out", default="output"); i.set_defaults(func=cmd_init)
    p = sub.add_parser("prompts"); p.set_defaults(func=cmd_prompts)
    v = sub.add_parser("validate"); v.add_argument("--dir", required=True); v.set_defaults(func=cmd_validate)
    s = sub.add_parser("stats"); s.add_argument("--dir", required=True); s.set_defaults(func=cmd_stats)
    d = sub.add_parser("diff"); d.add_argument("--prd", required=True); d.add_argument("--from", dest="from_dir", required=True); d.add_argument("--out", default="output"); d.add_argument("--product"); d.add_argument("--version", dest="ver"); d.set_defaults(func=cmd_diff)
    m = sub.add_parser("merge"); m.add_argument("--from", dest="from_dir", required=True); m.add_argument("--new", dest="new_dir", required=True); m.add_argument("--diff", dest="diff_file", required=True); m.add_argument("--out", required=True); m.set_defaults(func=cmd_merge)
    r = sub.add_parser("import-results"); r.add_argument("--cases", required=True); r.add_argument("--results", required=True); r.add_argument("--out", required=True); r.set_defaults(func=cmd_import_results)
    pc = sub.add_parser("probe-cache", help="探测 Provider Prompt 缓存（连发两次相同 system，观察 cache_read/creation 回显）")
    pc.add_argument("--provider", choices=["openai", "anthropic", "ollama"]); pc.add_argument("--api-base"); pc.add_argument("--model"); pc.set_defaults(func=cmd_probe_cache)
    e = sub.add_parser("promote-examples"); e.add_argument("--cases", required=True); e.add_argument("--out", required=True); e.add_argument("--design-logic", required=True); e.add_argument("--reviewed", action="store_true"); e.set_defaults(func=cmd_promote_examples)
    g.add_argument("--repro", action="store_true", help="可复现模式：三阶段温度归零（回归验收基线）")
    g.add_argument("--seed", type=int, help="采样种子（OpenAI 兼容端点生效；Anthropic/MiniMax 忽略）")
    cr = sub.add_parser("compare-runs", help="结构级复现对拍：同源且结构指纹一致 → 复现")
    cr.add_argument("dir_a", metavar="RUN_A"); cr.add_argument("dir_b", metavar="RUN_B"); cr.add_argument("--out"); cr.add_argument("--fail-on-drift", action="store_true"); cr.set_defaults(func=cmd_compare_runs)
    return parser

def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except KeyboardInterrupt: return 130
    except Exception as exc: print(f"✗ 未预期错误: {type(exc).__name__}: {exc}"); return 1

if __name__ == "__main__": sys.exit(main())
