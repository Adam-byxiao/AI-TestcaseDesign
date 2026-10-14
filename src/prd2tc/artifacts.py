"""阶段产物契约与**链路完备性**校验。

为什么需要
----------
原先「能否从中间阶段开始跑」的检查散落在 pipeline 的 if/else 里，且只有
`.exists()`：

    if request.stage == "3" and not plan_path.exists(): raise FileNotFoundError(...)

这留下三类**静默错误结果**：

1. **空产物也算通过** —— 一个 0 字节的 `测试方案.md` 满足 `exists()`，
   阶段 3 会据此生成用例，产物看似正常；
2. **残缺产物也算通过** —— `requirements.json` 里 `requirements: []`
   （或 JSON 被截断后又修好）会让阶段 3 在「没有需求」的前提下跑完；
3. **陈旧产物被静默复用** —— 改了 PRD 后直接 `--stage 3`，用的是**旧 plan**，
   产出与当前 PRD 不匹配却毫无提示。这是最危险的一类。

因此把「阶段依赖什么、产出什么、如何算完备」集中成**唯一契约**，
并在进入阶段前做 preflight，失败时给出**可操作**的修复建议。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

# 清单文件名**只有一处分发**：写入方 `io` 定义主/次，这里直接复用。
# 原先两处各自定义且**语义相反**（io: run-manifest 主 / artifacts: manifest 主）——
# 由于读写两侧都会兜底尝试两个名字，当时没有功能影响，
# 但一旦有人只写其一，两边对「哪个是权威」的理解就会分叉。故统一到写入方。
from prd2tc.io import LEGACY_MANIFEST_NAME, MANIFEST_NAME

# 阶段序号 → 语义名（与 service.stage_name 保持一致）
STAGE_NAMES = {"1": "analysis", "2": "plan", "3": "cases"}
STAGE_ORDER = ("1", "2", "3")


@dataclass(frozen=True)
class Artifact:
    """一个阶段产物的契约。"""
    key: str                    # 稳定标识（用于错误信息与测试）
    rel: str                    # 相对运行根目录的路径；`*` 表示 glob
    kind: str                   # markdown | json | json_glob
    stage: str                  # 产出它的阶段
    label: str                  # 中文名（给人看）
    min_bytes: int = 1          # 完备性下限（0 字节/纯空白视为缺失）
    require_field: str = ""     # json：要求该字段非空
    fix: str = ""               # 缺失时的修复建议

    @property
    def is_glob(self) -> bool:
        return self.kind == "json_glob"


ANALYSIS_MD = Artifact(
    "analysis_md", "analysis/需求分析报告.md", "markdown", "1", "需求分析报告",
    min_bytes=80,
    fix="先跑阶段 1（`--stage 1`），或检查该文件是否被清空")
REQUIREMENTS_JSON = Artifact(
    "requirements_json", "analysis/requirements.json", "json", "1", "结构化需求",
    min_bytes=2, require_field="requirements",
    fix="先跑阶段 1（`--stage 1`）；若文件存在但需求为空，说明阶段 1 未真正抽取到需求")
PLAN_MD = Artifact(
    "plan_md", "plan/测试方案.md", "markdown", "2", "测试方案",
    min_bytes=80,
    fix="先跑阶段 2（`--stage 2`），或检查该文件是否被清空")
CASES_JSON = Artifact(
    "cases_json", "cases/cases/*.json", "json_glob", "3", "用例集",
    min_bytes=2,
    fix="先跑阶段 3（`--stage 3`）")

BY_KEY: dict[str, Artifact] = {a.key: a for a in
                              (ANALYSIS_MD, REQUIREMENTS_JSON, PLAN_MD, CASES_JSON)}

# 每个阶段的**输入**（上游产物）与**输出**
STAGE_INPUTS: dict[str, tuple[Artifact, ...]] = {
    "1": (),                                        # 阶段 1 只依赖 PRD 本身
    "2": (ANALYSIS_MD, REQUIREMENTS_JSON),
    "3": (PLAN_MD, REQUIREMENTS_JSON),
}
STAGE_OUTPUTS: dict[str, tuple[Artifact, ...]] = {
    "1": (ANALYSIS_MD, REQUIREMENTS_JSON),
    "2": (PLAN_MD,),
    "3": (CASES_JSON,),
}


@dataclass
class Problem:
    """一个链路完备性问题。"""
    artifact: Artifact
    code: str          # missing | empty | unreadable | malformed | incomplete | stale
    detail: str
    fix: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.artifact.label}（{self.artifact.rel}）：{self.detail}；建议：{self.fix}"


def _resolve(root: Path, artifact: Artifact) -> list[Path]:
    if artifact.is_glob:
        return sorted(root.glob(artifact.rel))
    p = root / artifact.rel
    return [p] if p.exists() else []


def _check_one(root: Path, artifact: Artifact) -> list[Problem]:
    """校验单个产物：存在 → 非空 → 可解析 → 结构完备。"""
    paths = _resolve(root, artifact)
    if not paths:
        return [Problem(artifact, "missing",
                        "产物不存在" + ("（无可匹配文件）" if artifact.is_glob else ""),
                        artifact.fix)]

    # 空/极小的文件（含纯空白）视为未产出 —— `exists()` 会漏掉这种情况
    substantial = [p for p in paths if p.stat().st_size >= artifact.min_bytes]
    if artifact.kind == "markdown":
        substantial = [p for p in substantial if p.read_text(encoding="utf-8").strip()]
    if not substantial:
        sizes = ", ".join(f"{p.name}={p.stat().st_size}B" for p in paths[:3])
        return [Problem(artifact, "empty",
                        f"产物为空或过小（{sizes}），小于下限 {artifact.min_bytes}B",
                        artifact.fix)]

    if len(substantial) != len(paths):
        return [Problem(artifact, "empty", "部分模块产物为空或过小", artifact.fix)]
    for target in substantial if artifact.kind in ("json", "json_glob") else []:
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return [Problem(artifact, "malformed",
                            f"{target.name} 无法解析为 JSON：{type(exc).__name__}: {exc}",
                            f"重新生成该阶段产物（{artifact.stage}），文件可能被截断或写坏")]
        if artifact.require_field:
            items = data.get(artifact.require_field) if isinstance(data, dict) else None
            if not items:
                return [Problem(artifact, "incomplete",
                                f"{target.name} 的 `{artifact.require_field}` 为空 —— "
                                f"下游会在「没有需求」的前提下跑完，产物看似正常实则无效",
                                artifact.fix)]
    return []


def read_manifest_hash(root: Path) -> str:
    """读取运行清单里记录的 PRD 文本哈希（无则空串）。"""
    for name in (MANIFEST_NAME, LEGACY_MANIFEST_NAME):
        p = root / name
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            value = data.get("prdHash")
            if value:
                return str(value)
    return ""


def check_one(root: Path, artifact: Artifact, *, prd_hash: str = "",
              recorded_hash: str = "", allow_stale: bool = False) -> list[Problem]:
    """校验单个产物（存在 → 非空 → 可解析 → 结构完备）+ 可选的新鲜度检查。

    `recorded_hash` 由调用方**提前采样**（必须在清单被重置前读取）。
    为什么不由本函数自行读取：pipeline 在 `resume=False` 时会调用 `init_output`
    重写清单，从而抹掉「这些产物属于哪份 PRD」的记录 ——
    若在此处才读，拿到的永远是**刚写入的新哈希**，陈旧检测形同虚设
    （实测踩到：改了 PRD 直接 `--stage 3` 静默通过了）。
    """
    problems = _check_one(root, artifact)
    if problems or not prd_hash or allow_stale:
        return problems
    # 每阶段各自保留来源：重跑 analysis 不代表旧 plan 已属于新 PRD。
    for name in (MANIFEST_NAME, LEGACY_MANIFEST_NAME):
        path = root / name
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            recorded_hash = data.get("stages", {}).get(STAGE_NAMES[artifact.stage], {}).get("prdHash") or recorded_hash
            break
    if recorded_hash and recorded_hash != prd_hash:
        # 产物属于另一份 PRD：这是**静默错配**的典型来源 ——
        # 改了 PRD 却直接 `--stage 3`，会用旧方案生成与当前 PRD 不匹配的用例。
        return [Problem(
            artifact, "stale",
            f"该产物是为**另一份 PRD** 生成的"
            f"（记录 {recorded_hash[:12]}… ≠ 当前 {prd_hash[:12]}…）",
            "改为 `--stage all` 全量重跑；若确认要复用旧产物，"
            "显式加 `--allow-stale-artifacts`")]
    return []


def check_inputs(root: Path, stage: str, *, prd_hash: str = "",
                 recorded_hash: str = "", allow_stale: bool = False) -> list[Problem]:
    """校验进入 `stage` 所需的全部上游产物，返回问题清单（空 = 可安全进入）。

    注意：调用方需先排除「由本次运行的上游阶段产出」的那些输入
    （见 `pipeline.GenerateRequest` 的 preflight）——否则 `--stage all`
    会因阶段 1 尚未生成而误报。
    """
    problems: list[Problem] = []
    for artifact in STAGE_INPUTS.get(stage, ()):
        problems.extend(check_one(root, artifact, prd_hash=prd_hash,
                                  recorded_hash=recorded_hash, allow_stale=allow_stale))
    return problems


def format_problems(problems: list[Problem], stage: str) -> str:
    """把问题清单渲染成可读的报错文本（含阶段语义名与逐条建议）。"""
    name = STAGE_NAMES.get(stage, stage)
    lines = [f"阶段 {stage}（{name}）无法开始：上游产物不完备（{len(problems)} 项）", ""]
    for p in problems:
        lines.append(f"  ✗ {p}")
    first = problems[0].artifact.stage if problems else "1"
    lines += ["",
              f"提示：要**补跑**缺的上游阶段，用 `--from-stage {first}`（从这里跑到最后）；",
              f"      只跑阶段 {stage} 而不补上游，用 `--stage {stage}`（会复用已有上游产物）；",
              f"      要不要从头重来，用 `--stage all`（必要时加 `--force`）；",
              f"      若确认要复用旧产物，加 `--allow-stale-artifacts`。"]
    return "\n".join(lines)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def stages_to_run(stage: str, from_stage: str = "") -> list[str]:
    """按 `--stage` / `--from-stage` 语义算出**真正会执行**的阶段列表。

    - `--stage N`：只跑阶段 N（复用已有上游产物）—— 现有语义，服务侧依赖它
    - `--from-stage N`：从阶段 N 开始跑到最后（用户要的「从中间某步继续」）
    - `--stage all`：三个阶段全跑
    """
    if from_stage:
        if from_stage not in STAGE_ORDER:
            return []
        return [s for s in STAGE_ORDER if s >= from_stage]
    if stage == "all":
        return list(STAGE_ORDER)
    return [stage] if stage in STAGE_ORDER else []


def stage_plan(stage: str, from_stage: str = "") -> list[tuple[str, str]]:
    """返回 [(阶段序号, 动作)]，用于展示「哪些阶段会真跑」。"""
    running = set(stages_to_run(stage, from_stage))
    return [(s, "执行" if s in running else "复用已有产物") for s in STAGE_ORDER]
