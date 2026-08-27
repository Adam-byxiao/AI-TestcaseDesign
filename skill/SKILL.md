---
name: ai-test-case-design
description: 从 PRD 需求文档生成测试方案与测试用例。输入 PRD 路径与产品/版本，按「需求分析 → 测试方案 → 用例生成」三阶段 pipeline 产出评审稿与结构化 JSON（含质量自检与回溯表）。当任务涉及 PRD 分析、测试计划设计、测试用例生成、用例到 Playwright 骨架的衔接时使用。
---

# AI 测试用例设计（ai-test-case-design）

把 PRD 需求文档加工为「测试方案 + 测试用例集」的可复用 pipeline。产物写入 AiWebTestCenter 的
`01-AI-TestCaseDesign/output/<product>/<version>/` 目录。

## 何时使用

- 用户给出 PRD 文件（`PRD_File/<product>/PRD-*.md`），要求生成测试方案或测试用例。
- 需要从需求出发梳理测试范围、策略、优先级、风险。
- 需要把 PRD 需求点映射为可执行的自动化用例（供 Playwright 使用）。

## 输入

- `--prd`：PRD 文件路径（必填）。
- `--product`：产品代号，默认取 PRD 所在目录名。
- `--version`：版本号，默认从 PRD 文件名提取（如 `v0.3`）。
- 人工输入：评审意见、补充信息。

## 执行步骤（严格按顺序，每阶段结束向用户输出摘要并等待确认）

### 阶段 1：需求分析

1. 读取 PRD 全文。若未提供路径，先询问。
2. 按 `01-AI-TestCaseDesign/prompts/01-prd-analysis.md` 的指令完成分析：
   - 抽取功能模块（给模块代号）、角色、需求点（REQ-*）、业务规则（BR-*）、非功能需求（NFR-*）、风险点。
3. 将结果写入 `output/<product>/<version>/analysis/需求分析报告.md`。
4. 向用户输出摘要：模块数、需求点数、风险点；等待确认后再进入阶段 2。

### 阶段 2：测试方案

1. 以需求分析报告为输入，按 `prompts/02-test-plan.md` 生成测试方案：
   - 范围、策略分层、优先级口径、风险、环境数据、准入准出、用例量目标。
2. 按 `templates/test-plan.template.md` 排版，写入
   `output/<product>/<version>/plan/测试方案.md`。
3. 输出摘要（范围/策略/用例量目标），等待确认。

### 阶段 3：用例生成

1. 按 `prompts/03-test-cases.md` 逐模块生成用例，字段遵循
   `docs/case-template.md`（JSON schema 为准）。
2. 产物：
   - `output/<product>/<version>/cases/用例集.md`（评审稿，含 traceability 表）；
   - `output/<product>/<version>/cases/cases/<MODULE>.json`（每模块一个）。
3. 运行质量自检（见下），若不过则补齐后重跑。
4. 输出统计：用例总数、按模块/优先级分布、REQ 覆盖率、未覆盖清单。

### 收尾

- 更新 `output/<product>/<version>/run-log.md`（时间、PRD 版本、产物、评审意见）。
- 向用户汇总：产物路径、统计、待评审项。

## 质量自检（阶段 3 强制）

- [ ] 覆盖全部 REQ-*（缺失项列入未覆盖清单并说明原因）；
- [ ] 用例编号唯一、格式 `TC-<MOD>-<SEQ>`；
- [ ] 每条用例含 steps（元素+动作）与 expected（可断言）；
- [ ] P0 用例含 `@smoke` 标签；
- [ ] 无重复用例。

## 规则与约束

- 不臆造 PRD 中没有的需求；需求模糊处标注「待确认」，不得擅自扩展范围。
- 产物一律写入 output 目录，不修改 PRD 原文件。
- 若 PRD 缺少必需章节（概述/功能需求/业务规则），先告知用户补齐，不得跳步。
- 涉及金额/数量计算时，按 PRD 业务规则（BR-*）计算并在用例 testData 中给出预期值。
