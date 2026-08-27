"""prd2tc 命令行入口。

用法示例：
  # 全自动三阶段生成（需 DEEPSEEK_API_KEY）
  prd2tc generate --prd PRD_File/youxuan-mall/PRD-优选商城-v0.3.md \
    --out output --product youxuan-mall --version v0.3

  # 只看本轮要发给大模型的 prompt（不调用 API）
  prd2tc generate --prd <prd> --dry-run

  # 离线自测：用内置 stub 响应跑通三阶段（无需 API key）
  prd2tc generate --prd <prd> --stub --out /tmp/pipeline-test

  # 校验 / 统计已生成用例
  prd2tc validate --dir output/youxuan-mall/v0.3/cases/cases
  prd2tc stats    --dir output/youxuan-mall/v0.3/cases/cases
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from prd2tc import __version__, constraints, gates, loader
from prd2tc import stages as S
from prd2tc.config import Config
from prd2tc.io import init_output, render_cases_md, save_cases, stats, write_stage
from prd2tc.validator import ValidationError, validate_dir

# Windows 控制台默认 GBK；统一 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _extract_version(prd_text: str, prd_path: Path) -> str:
    m = re.search(r"版本[：:]\s*(v[\d.]+)", prd_text)
    if m:
        return m.group(1)
    m = re.search(r"PRD-[^-]+-(v[\d.]+)\.md$", prd_path.name)
    return m.group(1) if m else "v0.1"


def _stub_chat(cfg: Config, user_text: str, **kwargs):
    """离线自测用：按阶段关键词返回预置内容（不真实调用 API）。"""
    from prd2tc.llm import LLMResult

    if "任务指令" not in user_text:
        raise AssertionError("user 消息缺少任务指令")
    if "PRD 全文" in user_text:
        content = "# 需求分析报告（stub）\n\n## 1. 功能模块清单\n| 模块 | 说明 |\n| AUTH | 账号 |"
    elif "需求分析报告" in user_text and "测试方案排版模板" in user_text:
        content = "# 测试方案（stub）\n\n## 1. 测试范围\n- In Scope: AUTH\n\n## 2. 测试策略\n- 功能测试"
    else:
        content = (
            "```json\n"
            '[{"id":"TC-STUB-001","module":"AUTH","subModule":"STUB","title":"stub 用例",'
            '"priority":"P1","tags":["@regression"],"requirement":["REQ-STUB-001"],'
            '"steps":[{"action":"goto","target":"/","data":""}],"expected":["页面渲染"]}]\n'
            "```"
        )
    return LLMResult(content=content, model="stub")


def _build_llm(args) -> callable:
    if args.stub:
        return _stub_chat
    if args.dry_run:
        return None
    return S.chat_completion


def _load_examples(args, prd_text: str) -> list:
    """加载示例库并按与当前 PRD 的相关性检索 Top-K（--examples / PRD2TC_EXAMPLES）。"""
    from prd2tc.examples import load_examples, retrieve

    dir_arg = args.examples or __import__("os").environ.get("PRD2TC_EXAMPLES")
    if not dir_arg:
        return []
    examples = load_examples(dir_arg)
    k = args.examples_k if getattr(args, "examples_k", None) else 3
    picked = retrieve(examples, prd_text, k=k)
    if picked:
        print(f"📚 已注入 {len(picked)} 条参考示例: {[e.module for e in picked]}")
    return picked


def cmd_generate(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    if not args.stub and not args.dry_run and not cfg.api_key:
        print("✗ 未配置 API Key：设置环境变量 DEEPSEEK_API_KEY（或 --stub 离线自测 / --dry-run 预览）")
        return 2

    prd_text = loader.load_prd(args.prd)
    prd_path = Path(args.prd)
    product = args.product or prd_path.parent.name
    version = args.ver or _extract_version(prd_text, prd_path)
    root = Path(args.out) / product / version
    init_output(root, product, version, str(prd_path.resolve()))
    print(f"输出目录: {root}")

    llm = _build_llm(args)
    mode = "dry-run" if args.dry_run else ("stub" if args.stub else cfg.model)
    print(f"模式: {mode}  model: {cfg.model}")

    examples = _load_examples(args, prd_text) if not args.dry_run else []
    gate_failed: list[str] = []

    def _gated(stage_label: str, stage_no: int, build_user, gate_fn, fallback):
        """门禁开启时走 生成→校验→反馈修正；关闭（--no-gate/--stub）时直接调用 fallback。"""
        if args.no_gate or args.stub:
            content, result = fallback()
            return content, result, []
        content, result, errors = S.generate_with_feedback(
            cfg, build_user, gate_fn, llm=llm,
            max_retries=args.max_gate_retries, label=stage_label,
            system=S.SYSTEM_ROLES[stage_no],
            temperature=S.stage_temperature(stage_no, cfg),
        )
        if errors:
            gate_failed.append(stage_label)
            _report_gate(stage_label, errors, args)
        return content, result, errors

    # 阶段 1
    if args.stage in ("1", "all"):
        if args.dry_run:
            print(f"\n===== 阶段1 prompt（{S.PROMPT_1}）=====")
            print(S.build_stage_1_user(prd_text, examples=_load_examples(args, prd_text)))
        else:
            print("\n[1/3] 需求分析 …")
            content, result, errors = _gated(
                "阶段1 需求分析", 1,
                lambda fb: S.build_stage_1_user(prd_text, examples=examples, feedback=fb),
                lambda t: gates.check_analysis(t, prd_text),
                lambda: S.run_stage_1(cfg, prd_text, llm=llm, examples=examples),
            )
            analysis_md, r1 = content, result
            write_stage(root, "analysis", analysis_md)
            print(f"✔ 需求分析报告.md（tokens: {r1.total_tokens}）")
            _maybe_review(args)
    else:
        analysis_md = (root / "analysis" / "需求分析报告.md").read_text(encoding="utf-8")

    # 阶段 2
    if args.stage in ("2", "all"):
        if args.dry_run:
            print(f"\n===== 阶段2 prompt（{S.PROMPT_2}）=====")
            print(S.build_stage_2_user("…（此处为阶段 1 产出的需求分析报告）"))
            return 0
        print("\n[2/3] 测试方案 …")
        content, result, errors = _gated(
            "阶段2 测试方案", 2,
            lambda fb: S.build_stage_2_user(analysis_md, feedback=fb),
            gates.check_plan,
            lambda: S.run_stage_2(cfg, analysis_md, llm=llm),
        )
        plan_md, r2 = content, result
        write_stage(root, "plan", plan_md)
        print(f"✔ 测试方案.md（tokens: {r2.total_tokens}）")
        _maybe_review(args)
    else:
        plan_md = (root / "plan" / "测试方案.md").read_text(encoding="utf-8")

    # 阶段 3
    if args.stage in ("3", "all"):
        if args.dry_run:
            print(f"\n===== 阶段3 prompt（{S.PROMPT_3}）=====")
            print(S.build_stage_3_user("…（此处为阶段 2 产出的测试方案）", examples=_load_examples(args, prd_text)))
            return 0
        print("\n[3/3] 用例生成 …")
        if args.no_gate or args.stub:
            cases, r3 = S.run_stage_3(cfg, plan_md, llm=llm, examples=examples)
            errors = []
        else:
            content, result, errors = S.generate_with_feedback(
                cfg,
                lambda fb: S.build_stage_3_user(plan_md, examples=examples, feedback=fb),
                gates.check_cases_text,
                llm=llm,
                max_retries=args.max_gate_retries,
                label="阶段3 用例生成",
                system=S.SYSTEM_ROLES[3],
                temperature=S.stage_temperature(3, cfg),
            )
            r3 = result
            if errors:
                gate_failed.append("阶段3 用例生成")
                _report_gate("阶段3 用例生成", errors, args)
            else:
                cases = S.extract_cases(content)
        if not errors:
            save_cases(root / "cases" / "cases", cases)
            write_stage(root, "cases", render_cases_md(cases, plan_title="PRD v" + version))
            s = stats(cases)
            print(f"✔ 用例集.md + {len(s['by_module'])} 个模块 JSON（tokens: {r3.total_tokens}）")
            print(f"  用例 {s['total']} 条，优先级分布 {s['by_priority']}，覆盖 REQ {s['req_covered']} 个")
            if not args.stub:
                print("  ⚠ 请在 output 目录人工评审用例集.md 后再投入使用")
        else:
            print(f"  ✗ 阶段3 用例生成被门禁拦截，未写入用例文件")

    if gate_failed and not args.allow_gate_fail:
        print(f"\n✗ 质量门禁未全部通过（{', '.join(gate_failed)}）。"
              f"可用 --allow-gate-fail 忽略继续，或调整提示词/示例后重跑。")
        return 1
    print("\n完成。")
    return 0


def _report_gate(stage_label: str, errors: list[str], args: argparse.Namespace) -> None:
    if args.allow_gate_fail:
        print(f"  ⚠ [{stage_label}] 门禁未通过（{len(errors)} 项，--allow-gate-fail 已忽略）")
    else:
        print(f"  ✗ [{stage_label}] 门禁未通过（{len(errors)} 项，将反馈模型修正；重试后仍失败则拦截）")
    for e in errors[:8]:
        print(f"    - {e}")
    if len(errors) > 8:
        print(f"    … 等共 {len(errors)} 项")


def _maybe_review(args: argparse.Namespace) -> None:
    if args.review:
        input("  按 Enter 确认后进入下一阶段（Ctrl+C 中断）…")


def cmd_prompts(args: argparse.Namespace) -> int:
    """列出 prompt 资源版本与哈希（实证迭代：版本号 + 内容指纹可追溯）。"""
    import hashlib

    for name in sorted(loader.REPO_PROMPTS.glob("*.md"), key=lambda p: p.name):
        text = name.read_text(encoding="utf-8")
        ver = re.search(r"prompt v[\d.]+", text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        print(f"{name.name:36s} {ver.group(0) if ver else '未声明版本':>12s}  sha256={digest}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.out) / args.product / args.version
    init_output(root, args.product, args.version, args.prd)
    print(f"✔ 已初始化: {root}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        total, errors = validate_dir(args.dir)
    except FileNotFoundError as e:
        print(f"✗ {e}")
        return 1
    if errors:
        print(f"校验失败：{len(errors)} 个问题")
        for e in errors:
            print(f"  ✗ {e}")
        return 1
    print(f"✔ 校验通过：{total} 条用例，id 唯一，字段与枚举全部合法")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    try:
        total, errors = validate_dir(args.dir)
    except FileNotFoundError as e:
        print(f"✗ {e}")
        return 1
    if errors:
        print(f"✗ 目录含非法用例（{len(errors)} 个问题），请先运行 validate")
        return 1
    from prd2tc.io import stats as _stats

    cases = []
    for f in sorted(Path(args.dir).glob("*.json")):
        cases.extend(json.loads(f.read_text(encoding="utf-8").lstrip("\ufeff")))
    s = _stats(cases)
    print(f"用例总数: {s['total']}")
    print(f"按模块: {json.dumps(s['by_module'], ensure_ascii=False)}")
    print(f"按优先级: {json.dumps(s['by_priority'], ensure_ascii=False)}")
    print(f"按标签: {json.dumps(s['by_tag'], ensure_ascii=False)}")
    print(f"覆盖 REQ 数: {s['req_covered']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prd2tc",
        description="PRD → 测试方案 → 测试用例：大模型驱动的测试用例生成工具（零第三方依赖，默认 DeepSeek API）",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="PRD 生成需求分析/测试方案/用例集")
    g.add_argument("--prd", required=True, help="PRD 文件路径")
    g.add_argument("--product", help="产品代号（默认取 PRD 所在目录名）")
    g.add_argument("--version", dest="ver", help="版本号（默认从 PRD 头/文件名提取）")
    g.add_argument("--out", default="output", help="输出根目录（默认 output/）")
    g.add_argument("--stage", choices=["1", "2", "3", "all"], default="all", help="执行阶段")
    g.add_argument("--dry-run", action="store_true", help="只打印 prompt，不调用 API")
    g.add_argument("--stub", action="store_true", help="离线自测：用预置响应跑通全流程")
    g.add_argument("--review", action="store_true", help="每阶段结束暂停等待确认")
    g.add_argument("--examples", help="示例库目录（历史 PRD-用例对，注入 prompt 供模型学习）")
    g.add_argument("--examples-k", type=int, default=3, help="注入示例条数（默认 3）")
    g.add_argument("--max-gate-retries", type=int, default=2, help="门禁失败反馈重试次数（默认 2，0 为不重试直接拦截）")
    g.add_argument("--allow-gate-fail", action="store_true", help="门禁最终未通过时仅告警不失败")
    g.add_argument("--no-gate", action="store_true", help="关闭机器门禁（仅 prompt 自检）")
    g.set_defaults(func=cmd_generate, version_getter=lambda a: a.ver)

    i = sub.add_parser("init", help="初始化产物目录 + manifest")
    i.add_argument("--product", required=True)
    i.add_argument("--version", required=True, dest="ver")
    i.add_argument("--prd", help="PRD 路径（写入 manifest）")
    i.add_argument("--out", default="output")
    i.set_defaults(func=cmd_init)

    pr = sub.add_parser("prompts", help="查看 prompt 资源版本与内容哈希")
    pr.set_defaults(func=cmd_prompts)

    v = sub.add_parser("validate", help="校验用例 JSON（字段/id 唯一/枚举）")
    v.add_argument("--dir", required=True, help="cases/cases 目录")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("stats", help="用例统计")
    s.add_argument("--dir", required=True)
    s.set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except (FileNotFoundError, ValueError, ValidationError) as e:
        print(f"✗ {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"✗ 未预期错误: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
