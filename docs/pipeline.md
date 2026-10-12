# Pipeline 详细设计：PRD → 测试方案 → 测试用例

## 1. 设计原则

1. **人工可介入**：每个阶段产物都是可读 Markdown，供测试负责人评审修改后再进入下一阶段。
2. **可回溯**：用例 ↔ PRD 需求点（REQ-*）双向可追溯，覆盖率可统计。
3. **可机读**：最终用例集同时输出 JSON（结构化字段固定），供校验、统计、生成 Playwright 骨架。
4. **确定性**：PRD 结构（章节 + REQ 编号）是解析前提；脚本只做工程化，不臆造内容。

## 2. 阶段定义

### 阶段 1：需求分析（prompts/01）

**输入**：PRD 全文（markdown）。
**任务**：
- 抽取功能模块清单（含模块代号，见 conventions）。
- 抽取角色清单。
- 逐条整理需求点：编号、描述、验收标准、异常分支。
- 抽取业务规则（BR-*）与非功能需求（NFR-*）。
- 标注需求风险点（模糊、依赖外部、易回归）。

**输出**：`output/<product>/<version>/analysis/需求分析报告.md`
**质量自检**：
- 每个 REQ 编号与 PRD 一致、无遗漏；
- 验收标准可测试（有明确可断言点）。

### 阶段 2：测试方案（prompts/02）

**输入**：需求分析报告。
**任务**：
- 测试范围（In/Out Scope）。
- 测试策略分层：功能（UI 主链路）/ 边界与异常 / 权限与状态 / 兼容（三浏览器）/ 性能冒烟。
- 优先级分配口径（P0–P3，见 conventions）。
- 风险与应对。
- 测试环境与数据（被测地址、账号、mock 说明）。
- 准入/准出标准。
- 工作量粗估（用例数目标）。

**输出**：`output/<product>/<version>/plan/测试方案.md`（按 `templates/test-plan.template.md`）

### 阶段 3：用例生成（prompts/03）

**输入**：测试方案。
**任务**：按模块生成用例，覆盖矩阵：

| 维度 | 覆盖 |
| --- | --- |
| 正向 | 主流程 happy path（P0） |
| 反向 | 错误输入/非法操作 |
| 边界 | 边界值（价格、数量、金额临界） |
| 异常 | 空态、无权限、状态异常（如停用账号） |
| 兼容 | 浏览器差异（结合 PRD 兼容要求） |
| 权限 | 游客/登录用户/停用用户 |

**输出**：
- `output/<product>/<version>/cases/用例集.md`（评审稿，按模块分节）；
- `output/<product>/<version>/cases/cases/*.json`（每模块一个 JSON，字段见 case-template.md）；
- `output/<product>/<version>/cases/<product>-<version>-用例集.xlsx`（Excel：用例明细 / 需求覆盖 / 统计概览 三个 sheet）；
- `output/<product>/<version>/cases/<product>-<version>-用例集.xmind`（思维导图：模块 → 子模块 → 用例）；
- 可选：`output/<product>/<version>/cases/manifest.json`（汇总统计）。

> 后两种格式由阶段 3 成功后**自动导出**（也可用 `prd2tc export` 单独重跑）。
> 导出失败只降级为告警，不影响生成结果；实现细节见 `docs/export-excel-xmind.md`。

**质量自检（强制）**：
- [ ] 覆盖全部 REQ-*（生成 traceability 映射表，标注未覆盖项）；
- [ ] 用例编号全局唯一、符合 `TC-<MOD>-<SEQ>`；
- [ ] 每条用例含：前置、步骤（元素+动作）、预期（可断言）、优先级、标签；
- [ ] P0 用例带 `@smoke` 标签；
- [ ] 无重复用例（同步骤+同预期去重）。

## 3. 产物规范

- 目录：`output/<product>/<version>/{analysis,plan,cases}/`。
- 用例 JSON 的字段与取值枚举以 `docs/case-template.md` 为准，脚本 `validate` 强校验。
- `orchestrate.mjs init` 生成目录与 `manifest.json` 骨架。
- `analysis/requirements.json` 是结构化需求事实源，`cases/traceability.json` 保存 REQ↔TC 双向追踪矩阵。
- `run-manifest.json` 是运行清单事实源；`manifest.json` 仅作为旧版本兼容副本同步维护。
- LLM 通过统一 `LLMClient` 接口接入 `openai`、`anthropic`、`ollama`；核心使用标准库 HTTP，不强制安装第三方 SDK。
- `run-manifest.json` 保存本次运行的输入哈希、模型参数、阶段统计和状态；JSON 文件使用原子写入。

## 4. 与 Playwright 的衔接（下一迭代）

- 用例 JSON `steps[].action` 枚举建议对齐 Playwright 动作：
  `goto / fill / click / selectOption / check / hover / expectVisible / expectText / expectUrl / waitFor`。
- `expected` 建议对齐断言：可见性、文本、URL、状态。
- 生成器（`scripts/generate-spec.mjs` 预留）把用例 JSON 转成 `tests/<module>/*.spec.ts` 骨架 + 引用 `src/pages/*` 的占位调用。

## 5. 执行记录

- 每次 pipeline 运行在 `output/<product>/<version>/run-log.md` 记录：执行时间、PRD 版本、阶段产物、评审意见。
- 可选执行反馈使用 `schemaVersion=1.0` 的 `{runId, results[]}` 格式，结果项至少包含 `caseId`、`status` 和 `durationMs`。

## 6. 长文档、恢复与增量更新

- 阶段 1 对长 PRD 分段并发抽取，再执行全量合并门禁；阶段 3 按结构化需求模块并发生成。
- `--resume` 只复用 PRD、Prompt、模型、关键参数和示例指纹完全一致的成功模块检查点。
- `prd2tc diff` 输出需求四分类，`prd2tc merge` 将旧用例迁移为 `active/needs_review/deprecated/superseded`，历史记录不物理删除。
- 02 子项目用 `scripts/export_results.py` 把 JUnit 转为执行契约，01 用 `prd2tc import-results` 导入到用例副本。
- `prd2tc promote-examples --reviewed` 只导出人工确认且最近执行通过的 active/reviewed 用例，避免失败原因未分类的结果污染示例库。

## 7. 输出截断与失败产物的处置

### 7.1 截断必须视为失败（不是「部分成功」）

`max_tokens` 用尽时 Provider 返回 `finish_reason=length`，此时输出**不完整**。必须注意一个陷阱：

> `extract_cases` 的「修补截断 JSON」回退会 `rfind("]")` 定位数组结尾。由于用例内部必然含
> `]`（如 `"tags":["@smoke"]`），当截断点落在「某个完整用例之后、某个内层数组闭合之后」时，
> 修补会产出一个**语法合法但内容残缺**的数组 → 字段级门禁全部通过 → 模块被误判为成功。

实测后果（运行 9d97060e，MiniMax-M3）：AGENT 6 条需求只留 1 条用例、SCENE 7 条需求只留 2 条，
5 个模块全被标记 `succeeded`，直到最后 traceability 才发现 3 条 P0 未覆盖而整条运行失败。

因此门禁的处置是：

1. **`finish_reason=length` 一律报错**，无论修补后的内容是否合法；
2. 出错时**自动把该批对半拆分重试**（`MAX_BATCH_SPLIT_DEPTH=2`）——降低单次输出量是唯一有效杠杆；
3. 若拆到单条需求仍截断，则模块标记 `failed`，可用「仅重试失败模块」局部恢复。

### 7.2 批次容量与输出预算（三者必须同时调）

`max_tokens`（默认 **16384**）、`TOKENS_PER_REQ_EST`（默认 **1800**）、`OUTPUT_BUDGET_RATIO`
（默认 **0.60**）、`CASE_BATCH_CAP`（默认 **8**）共同决定「单次请求的输出是否有余量」：

```
每批需求数 = min( max_tokens × OUTPUT_BUDGET_RATIO ÷ TOKENS_PER_REQ_EST , CASE_BATCH_CAP , 模块需求数 )
```

> ⚠️ **关键陷阱：只调高 `max_tokens` 不会降低截断率。**
> 批次容量与 `max_tokens` 成正比，若 `OUTPUT_BUDGET_RATIO` 沿用 0.85，
> 调高上限只会让每批容纳更多需求，安全余量恒为 `1/0.85 ≈ 1.18x`。
> 必须同时下调预算占用比，才能把上限换成真实余量。`tests/test_long_doc.py`
> 的 `test_raising_max_tokens_actually_buys_headroom` 会强制守住这一点。

实测校准（运行 9d97060e，MiniMax-M3）：

| 模块 | 需求数 | 输出 token | tok/需求 | 状态 |
|---|---|---|---|---|
| KB | 20 | 34005 | 1700 | 完整 |
| AGENT | 6 | 16384 | ≥2731 | 截断 |
| SCENE | 7 | 8192 | ≥1170 | 截断 |
| TEST | 8 | 8192 | ≥1024 | 截断 |
| TOOL | 3 | 8192 | ≥2731 | 截断 |

现配置下每批 5 条需求，预期输出 9000，余量 **1.82x**；即使按最坏值 2731 tok/需求
估算（13655）也仍在 16384 之内。叠加「截断即拆批」自愈，残差风险由重试兜底。

调参入口：`PRD2TC_MAX_TOKENS`、`PRD2TC_TOKENS_PER_REQ`、`PRD2TC_CASE_BATCH_CAP`、
`PRD2TC_CASE_OUTPUT_BUDGET`、`PRD2TC_CASE_BATCH_SIZE`（硬覆盖每批条数）。

### 7.3 失败时的产物保全

覆盖率门禁失败时，已生成的用例**不再被丢弃**，而是写入 `cases/partial/` 并附
`READ-ME-部分用例未通过门禁.md`（含门禁错误清单与处置建议）；`run-manifest.json` 的
`stages.cases.partialCaseCount` 记录数量。

正式产物 `cases/cases/` 在门禁未通过时保持为空，**避免失败产物被误当作交付结果**。
`cases/partial/` 会出现在产物清单里，便于下载后人工修复。
