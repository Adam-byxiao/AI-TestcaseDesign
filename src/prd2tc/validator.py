"""用例 JSON 校验（与 docs/case-spec.md 字段规范一致）。

规则与 scripts/orchestrate.mjs 对齐：
  - id 必填且唯一，格式 TC-<MOD>[-<SUB>]-<3位序号>
  - module ∈ 模块表；priority ∈ P0..P3
  - P0 必带 @smoke 标签；tags/requirement/steps/expected 非空
  - step.action ∈ 动作枚举，step.target 非空
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from prd2tc.actions import ASSERTIONS, is_assertion
from prd2tc.schema import (ACTION_SCHEMA_VERSION, ACTIONS_BY_VERSION, DESIGN_METHODS,
                           validate_step_data, validate_test_data)

MODULES = [
    "AUTH", "HOME", "PROD", "SEARCH", "CART", "CHECKOUT", "COUPON",
    "ORDER", "ACCOUNT", "COMMENT", "COMMON", "PERF",
    "CHK", "GLB", "DET", "LIST", "ERR",
]
PRIORITIES = ["P0", "P1", "P2", "P3"]
CASE_STATUSES = ["active", "needs_review", "deprecated", "superseded", "draft", "reviewed", "obsolete"]
ACTIONS = sorted(ACTIONS_BY_VERSION[ACTION_SCHEMA_VERSION])
ID_RE = re.compile(r"^TC-[A-Z][A-Z0-9_]*(-[A-Z0-9_]+)?-\d{3}$")
REQ_RE = re.compile(r"^REQ-[A-Z][A-Z0-9_]*-\d{3}$")

# ---- 约束实现（对应 prompts/00-testing-principles.md 的 [必须]/[禁止] 条目）----
# 判据与提示词中的示例必须一致（见 docs/prompts-and-gates.md 的「三层约束体系」）。

# C-006 禁止不可验证预期（00-testing-principles.md §4）
# 空洞断言词表：命中后仍需长度保护，避免误伤「页面跳转正常，提示"余额不足"」这类含具体内容的表述。
VAGUE_EXPECTED_TOKENS = (
    "表现正常", "显示正常", "功能正常", "运行正常", "工作正常",
    "无异常", "没有异常", "正常显示", "符合预期", "正确显示",
    "显示正确", "功能可用", "无报错", "不报错",
)
_VAGUE_MAX_LEN = 12       # 长度保护：超过此长度视为含具体内容
# 预期长度下限（len(text) <= 该值 且不含数字 → 判过短）。
# 取值 8 是**有意从严**（用户指定）：把「页面渲染」「合计归零」这类 4~6 字短语挡在门外，
# 要求预期写成完整可断言的句子。
# 代价（实测 18 条真实样本）：拦截率 38.9%，含示例库 2 条。
# 因此示例库 / prompt 示例 / 文档必须同步改为长句——否则模型照抄示例即被拦，
# 形成「示例→门禁→重试」死循环（见 docs/prompts-and-gates.md 的事故记录）。
_MIN_EXPECTED_LEN = 8

# C-007 禁止重复用例（00-testing-principles.md §4）
# 判定口径与 prompt 完全一致：模块 + 步骤序列 + 预期序列 三者全同才算重复。
_DUP_WS_RE = re.compile(r"\s+")


def _dup_norm(text) -> str:
    return _DUP_WS_RE.sub("", str(text or "")).lower()


def case_signature(case: dict) -> str:
    """用例去重签名：模块 + 步骤序列（动作>目标）+ 预期序列。

    刻意不含 data：prompt 的判定口径是「同步骤 + 同预期」，
    同一操作序列换输入数据仍属「为凑数量的同质用例」。
    """
    steps = "|".join(
        f"{_dup_norm(s.get('action'))}>{_dup_norm(s.get('target'))}"
        for s in (case.get("steps") or []) if isinstance(s, dict)
    )
    expected = "|".join(_dup_norm(e) for e in (case.get("expected") or []))
    raw = f"{_dup_norm(case.get('module'))}#{steps}#{expected}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_vague_expected(text: str) -> bool:
    """空洞断言检测：词表命中 + 长度保护。"""
    if len(text) > _VAGUE_MAX_LEN:
        return False
    return any(token in text for token in VAGUE_EXPECTED_TOKENS)


def check_expectations(case: dict, at: str) -> list[str]:
    """expected 的可验证性校验（空 / 空洞 / 过短）。"""
    expected = case.get("expected")
    if not isinstance(expected, list) or not expected:
        return [f"{at} expected 为空"]
    errors: list[str] = []
    for item in expected:
        text = str(item).strip()
        if not text:
            errors.append(f"{at} expected 含空字符串")
            continue
        if is_vague_expected(text):
            errors.append(
                f"{at} 预期不可验证（空洞断言）: {text!r}；"
                f"请改为可断言的表述（具体 UI 元素/数据/状态/金额）"
            )
        elif len(text) <= _MIN_EXPECTED_LEN and not any(ch.isdigit() for ch in text):
            errors.append(f"{at} 预期过短且无具体值: {text!r}；请补充具体断言对象")
    return errors


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"{len(errors)} 个校验问题")


def format_exception(exc: BaseException) -> str:
    """把异常渲染为可持久化的错误文本，并保留携带明细的 .errors。

    ValidationError 的 str() 只有「N 个校验问题」，真实原因在 .errors 里；
    直接格式化会丢失「HTTP 404 / 模型名不合法」这类唯一诊断线索。
    """
    errors = getattr(exc, "errors", None)
    if errors:
        detail = "；".join(str(e) for e in errors)
        return f"{type(exc).__name__}: {detail}"
    return f"{type(exc).__name__}: {exc}"


def validate_case(case: dict, index: int, seen_ids: set[str], allowed_modules: set[str] | None = None,
                  *, require_design_method: bool = True) -> list[str]:
    """校验单条用例，返回错误列表（尽可能收集全部问题）。

    require_design_method=False 可放宽 C-005，用于读取未按新契约产出的历史用例。
    """
    errors: list[str] = []
    at = f"[用例#{index + 1} {case.get('id', '(无id)')}]"

    cid = case.get("id")
    if not cid or not isinstance(cid, str):
        errors.append(f"{at} 缺少 id")
    else:
        if not ID_RE.match(cid):
            errors.append(f"{at} id 格式非法: {cid}")
        if cid in seen_ids:
            errors.append(f"{at} id 重复: {cid}")
        seen_ids.add(cid)

    modules = allowed_modules or set(MODULES)
    if case.get("module") not in modules:
        errors.append(f"{at} module 非法: {case.get('module') or '(空)'}")
    if case.get("priority") not in PRIORITIES:
        errors.append(f"{at} priority 非法: {case.get('priority') or '(空)'}")
    # C-005 design_method 必须标注（00-testing-principles.md §2 [必须]）。
    # 收紧此项时同步在 03-test-cases.md 的示例里补上该字段——示例优先于描述，
    # 否则模型会服从示例而漏字段，形成「示例→门禁→重试」的死循环。
    if require_design_method:
        method = case.get("design_method")
        if not method:
            errors.append(f"{at} 缺少 design_method（必须标注测试设计方法）")
        elif str(method) not in DESIGN_METHODS:
            errors.append(f"{at} design_method 非法: {method}；可选值 {list(DESIGN_METHODS)}")
    if case.get("status") is not None and case.get("status") not in CASE_STATUSES:
        errors.append(f"{at} status 非法: {case.get('status')}")
    tags = case.get("tags")
    if not isinstance(tags, list) or len(tags) == 0:
        errors.append(f"{at} tags 为空")
    # `@smoke` 的**方向**很关键：不是「P0 必带」，而是「只有 P0 才可带」。
    # 此前规则写成「P0 必带 @smoke」，使该标签与 P0 1:1 等同、不承载任何信息，
    # 实测评测 v2 的冒烟集膨胀到全部用例的 74%、v3 达 25%。
    # 现在 @smoke 是「冒烟集」——P0 的**真子集**（由 priority.select_smoke 判定）。
    if isinstance(tags, list) and "@smoke" in tags and case.get("priority") != "P0":
        errors.append(
            f"{at} @smoke 只能标在 P0 用例上（当前 priority={case.get('priority') or '(空)'}）；"
            f"@smoke 表示「部署后最先跑的最小集合」，是 P0 的子集而非全部 P0")

    reqs = case.get("requirement")
    if not isinstance(reqs, list) or len(reqs) == 0:
        errors.append(f"{at} requirement 为空")
    for r in reqs or []:
        if not REQ_RE.match(str(r)):
            errors.append(f"{at} requirement 格式非法: {r}")

    steps = case.get("steps")
    if not isinstance(steps, list) or len(steps) == 0:
        errors.append(f"{at} steps 为空")
    for s in steps or []:
        if not isinstance(s, dict):
            errors.append(f"{at} step 不是对象")
            continue
        if s.get("action") not in ACTIONS:
            errors.append(
                f"{at} step.action 非法: {s.get('action') or '(空)'}；"
                f"可选 {sorted(ACTIONS)}")
        if not s.get("target"):
            errors.append(f"{at} step.target 为空")
    # 断言存在性：**每条用例至少 1 个断言动作**。
    # 否则用例只有 goto/fill/click，无法判定通过/失败，却完全合规
    # （实测评测 v1/v2/v3 分别有 5/4/1 条这样的用例）。
    # 注：`waitFor` 是**同步动作**（等待就绪），不算断言 —— 这是常见误解。
    if isinstance(steps, list) and steps:
        if not any(is_assertion(s.get("action")) for s in steps if isinstance(s, dict)):
            errors.append(
                f"{at} 缺少断言步骤：用例必须至少包含一个断言动作"
                f"（{'/'.join(sorted(ASSERTIONS))}）；"
                f"仅用 click/fill/goto/waitFor 无法判定通过或失败")

    expected = case.get("expected")
    errors.extend(check_expectations(case, at))
    if not case.get("title"):
        errors.append(f"{at} 缺少 title")
    # testData / steps[].data 的嵌套是 JSON 转义崩坏的首要来源，必须拒绝并反馈重出
    if "testData" in case:
        errors.extend(f"{at} {issue}" for issue in validate_test_data(case.get("testData")))
    errors.extend(f"{at} {issue}" for issue in validate_step_data(case))
    return errors


def validate_dir(cases_dir: str | Path, allowed_modules: set[str] | None = None,
                 *, check_duplicates: bool = True,
                 require_design_method: bool = True) -> tuple[int, list[str]]:
    """校验目录下所有用例 JSON（顶层为数组或 cases 对象）。返回 (用例数, 错误列表)。

    - 刻意不做 testData 归一化：校验器的职责是**报告**问题（含嵌套这种会写坏 JSON 的形态），
      归一化由管线在落盘前完成。
    - C-007 重复检测是**跨文件**的：同模块用例可能分布在不同 JSON 文件里。
    """
    d = Path(cases_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"目录不存在: {d}")
    seen: set[str] = set()
    signatures: dict[str, str] = {}   # 签名 -> 首次出现的用例 id
    errors: list[str] = []
    total = 0
    smoke_ids: list[str] = []
    p0_ids: list[str] = []
    for f in sorted(d.glob("*.json")):
        try:
            raw = json.loads(f.read_text(encoding="utf-8").lstrip("\ufeff"))
        except json.JSONDecodeError as e:
            errors.append(f"[文件 {f.name}] JSON 解析失败: {e}")
            continue
        if isinstance(raw, dict) and isinstance(raw.get("cases"), list):
            cases = raw["cases"]
        elif isinstance(raw, list):
            cases = raw
        else:
            errors.append(f"[文件 {f.name}] 顶层必须是数组或 cases 对象")
            continue
        for i, case in enumerate(cases):
            at = f"[文件 {f.name} 用例#{i + 1} {case.get('id', '(无id)')}]"
            errors.extend(validate_case(case, total + i, seen, allowed_modules,
                                        require_design_method=require_design_method))
            if check_duplicates:
                errors.extend(check_duplicate(case, at, signatures))
            if case.get("priority") == "P0":
                p0_ids.append(str(case.get("id") or at))
            if isinstance(case.get("tags"), list) and "@smoke" in case["tags"]:
                smoke_ids.append(str(case.get("id") or at))
        # 必须在外层文件循环内、内层用例循环外：放进内层会让 total 按 len(cases) 倍膨胀
        total += len(cases)

    # 冒烟集的**全局**约束（单条用例层面看不出来，必须整体统计）
    errors.extend(check_smoke_set(smoke_ids, p0_ids, total))
    return total, errors


def check_smoke_set(smoke_ids: list[str], p0_ids: list[str], total: int) -> list[str]:
    """校验冒烟集的整体规模。

    `@smoke` = 部署后最先跑的**最小**集合。它的价值来自「小」——
    一旦规模逼近全量，就丧失了「快速判断这个版本能不能继续测」的作用。
    因此除单条用例的「只能标在 P0 上」之外，还要在**整体层面**约束：
      1. 有 P0 用例时冒烟集不能为空（否则失去快速门禁的意义）；
      2. 规模不得超过 `min(SMOKE_MAX, SMOKE_RATIO × 总数)`。
    """
    from prd2tc.priority import SMOKE_MAX, SMOKE_RATIO

    errors: list[str] = []
    if not p0_ids:
        return errors
    if not smoke_ids:
        errors.append(f"冒烟集为空：存在 {len(p0_ids)} 条 P0 用例，但没有任何 @smoke —— "
                      f"@smoke 表示「部署后最先跑的最小集合」，应当从 P0 中选取")
        return errors
    limit = max(1, min(SMOKE_MAX, int(total * SMOKE_RATIO)))
    if len(smoke_ids) > limit:
        errors.append(
            f"冒烟集过大：{len(smoke_ids)} 条 @smoke 超过上限 {limit} 条"
            f"（= min({SMOKE_MAX}, {int(SMOKE_RATIO * 100)}% × {total} 条用例)）；"
            f"@smoke 是「最小可跑集合」，不是「全部 P0」——"
            f"请只保留主干用例（每需求最多 1 条，排除边界与异常）")
    return errors


def check_duplicate(case: dict, at: str, signatures: dict[str, str]) -> list[str]:
    """C-007 重复用例检测：把签名登记进 signatures，命中则返回错误。

    signatures 由调用方在同一次批量校验内共享（跨文件/跨批次）。
    """
    signature = case_signature(case)
    first = signatures.get(signature)
    if first is not None:
        return [f"{at} 与用例 {first} 重复（同模块 + 同步骤 + 同预期）；请删除其一，或改用不同的步骤/预期"]
    signatures[signature] = str(case.get("id", ""))
    return []
