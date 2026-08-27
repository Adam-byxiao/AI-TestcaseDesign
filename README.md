# 01-AI-TestCaseDesign / prd2tc

**PRD → 测试方案 → 测试用例** 的独立开源工具：由大模型（默认 DeepSeek API）三阶段自动化生成，
产物为可评审的 Markdown + 结构化用例 JSON（供 Playwright 测试体系消费）。

> 定位：**独立的大模型驱动生成工具**，不依赖本地 DSH agent，可在任何服务器/CI 上无人值守运行。
> 技术栈：Python 3.10+，**零第三方依赖**（标准库 urllib 调 OpenAI 兼容 Chat Completions API）。

## 双运行模式

| 模式 | 说明 | 适用 |
| --- | --- | --- |
| **prd2tc CLI（推荐）** | 独立命令行工具，直接调大模型 API，全自动三阶段 | 线上/CI/批量 |
| **DSH skill（agent 变体）** | `skill/SKILL.md` 装载进 agent 逐阶段执行，人工可介入 | 本地深度使用/评审 |

两者共用同一套 `prompts/`、`templates/` 与字段规范（`docs/case-template.md`）。

## 安装

```bash
cd 01-AI-TestCaseDesign
pip install .            # 或开发模式 pip install -e .
export DEEPSEEK_API_KEY=sk-xxx   # 必填（--stub/--dry-run 可豁免）
```

## 快速开始

```bash
cd 01-AI-TestCaseDesign   # 或 pip install . 后在任意目录使用
pip install .             # 或开发模式 pip install -e .
export DEEPSEEK_API_KEY=sk-xxx   # 必填（--stub/--dry-run 可豁免）

# 全自动三阶段：PRD → 需求分析 → 测试方案 → 用例集（用仓库内置示例 PRD）
prd2tc generate --prd examples/sample-prd.md \
  --product sample-mall --version v0.1 --out output

# 只预览发给大模型的 prompt（不调用 API）
prd2tc generate --prd examples/sample-prd.md --dry-run

# 离线自测：用内置 stub 响应跑通全流程（无需 API key）
prd2tc generate --prd examples/sample-prd.md --stub --out /tmp/demo

# 校验 / 统计
prd2tc validate --dir output/sample-mall/v0.1/cases/cases
prd2tc stats    --dir output/sample-mall/v0.1/cases/cases
```

> 你自己的 PRD 只需满足推荐章节（概述/角色/功能需求/业务规则/非功能需求）即可，格式参考 `examples/sample-prd.md`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` / `PRD2TC_API_KEY` | - | API Key（必填） |
| `PRD2TC_API_BASE` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `PRD2TC_MODEL` | `deepseek-chat` | 模型名 |
| `PRD2TC_TEMPERATURE` | `0.4` | 采样温度 |
| `PRD2TC_TIMEOUT` | `120` | 请求超时（秒） |
| `PRD2TC_MAX_RETRIES` | `3` | 失败重试次数（指数退避） |

## 三阶段 Pipeline

| 阶段 | Prompt | 输入 → 输出 |
| --- | --- | --- |
| 1 需求分析 | `prompts/01-prd-analysis.md` | PRD → `analysis/需求分析报告.md` |
| 2 测试方案 | `prompts/02-test-plan.md` + 模板 | 分析报告 → `plan/测试方案.md` |
| 3 用例生成 | `prompts/03-test-cases.md` + 字段约束 | 测试方案 → `cases/用例集.md` + `cases/cases/<MODULE>.json` |

产物目录：`output/<product>/<version>/{analysis,plan,cases}/`（含 `manifest.json`）。
阶段 3 输出的用例 JSON 经内置校验（id 唯一、字段枚举、P0 必带 `@smoke`）后落盘，非法直接报错。

## 学习能力（示例库 + 检索注入，RAG-lite）

让 agent 从**已有的 PRD-用例对**中学习测试设计逻辑，而不是每次从零设计。

```bash
# 指定示例库（历史 PRD-用例对），按与当前 PRD 的相关性自动检索 Top-K 注入 prompt
prd2tc generate --prd <prd> --examples examples/ --examples-k 3
# 或环境变量
export PRD2TC_EXAMPLES=examples/
```

**工作机制**：
1. 示例文件（`examples/*.json`，数组）形如：`{module, title, keywords, requirement, design_logic, cases[]}`；
   `design_logic` 字段即"设计逻辑"——为什么这么设计（P0 必带 @smoke、反向断言、金额边界…），是模型学习的关键。
2. 相关性检索：模块命中 + 关键词覆盖 + 需求要点重合的轻量打分（零依赖），取 Top-K 且分数>0 的示例。
3. 注入：示例块进入**阶段 1（需求分析）与阶段 3（用例生成）** 的 prompt，模型按示例的模块划分、优先级口径、字段格式与设计逻辑完成任务。

预置示例：`examples/youxuan-mall-examples.json`（AUTH/CART/CHECKOUT 三组，从本项目真实产出提炼）。
新增示例：把你们已有的「需求点 + 测试用例」按格式导入即可，越多覆盖越广。

**设计依据（调研）**：
- [Reference-Based Retrieval-Augmented Unit Test Generation（ASE 2025）](https://conf.researchr.org/details/ase-2025/ase-2025-journal-first-track/46/Reference-Based-Retrieval-Augmented-Unit-Test-Generation)：历史参考用例做 RAG 是有效路径。
- [阿里云实践：RAG+大模型实现分钟级测试用例生成](https://developer.aliyun.com/article/1690088)：业界落地案例。
- [TestBrain](https://github.com/MangoFisher/TestBrain)（DeepSeek + RAG 知识库 + 用例评审）、[spec2case](https://github.com/blackhaiyu-sudo/spec2case)（生产级 agent + 人工确认 + 编排）等开源项目均采用知识库/示例注入路线。

## Prompt 体系（基于传统测试原理优化）

### Prompt 工程十原则落地情况

| 原则 | 落地实现 |
| --- | --- |
| 1 具体且无歧义 | 章节标题固定、数量一致性（REQ 一一对应）、金额精确值格式 |
| 2 展示而非陈述 | 阶段 1/3 prompt 内嵌少样本示例；阶段 3 另注入示例库（--examples） |
| 3 显式定义输出格式 | 阶段 1/2 固定六/七章节结构；阶段 3 指定 JSON 数组 + 字段约束（与 validator 同源） |
| 4 输入数据分隔符 | `build_user_message` 用 `<key>…</key>` 包裹全部输入数据，与指令隔离 |
| 5 指定角色 | 分阶段 system：测试架构师 / 测试经理 / 测试设计专家 |
| 6 指明不要做什么 | 每阶段「禁止」清单（不输出思考过程、不臆造、不省略章节、不输出非 JSON） |
| 7 思维链 | 每阶段「处理步骤」逐步执行；阶段 3 隐藏推理只输出结果 |
| 8 合理控制温度 | 阶段 1/2/3 默认 0.2/0.3/0.5（`PRD2TC_TEMP_S1/S2/S3` 可覆盖） |
| 9 基于实证迭代 | prompt 资产带 `> prompt vX.Y.Z` 版本头；`prd2tc prompts` 查看版本与内容哈希；编辑→升版→对比产物即 A/B |
| 10 近因偏差 | 任务指令→输入数据→**输出要求置末**（`# 输出要求` 拆分，门禁反馈追加在最后） |

### Prompt 资源清单

| 资源 | 作用 | 注入阶段 |
| --- | --- | --- |
| `prompts/00-testing-principles.md` | 共享测试原理层（**硬约束**）：约束分级（[必须]/[禁止]/[尽量]）、需求可测性条件分支、设计方法选用条件、覆盖维度、用例质量属性、优先级与风险、**逐阶段输出自检门** | 1 / 2 / 3 任务指令区 |
| `prompts/01~03` | 各阶段任务指令（角色/任务/处理步骤/输出要求），统一按 `# 输出要求` 拆分置末 | 对应阶段 |
| `prompts/04-case-constraints.md` | 用例生成约束模板：**静态约束** + **学习约束**（`{{learned_patterns}}` 由示例库动态填充） | 3 输出要求区 |

## 机器强制门禁（三层约束的最后一层）

prompt 里的"自检门"是**靠模型自觉**；prd2tc 还提供**程序化门禁**（`src/prd2tc/gates.py`），让门禁变成机器强制：

| 阶段 | 门禁校验项 | 失败处理 |
| --- | --- | --- |
| 1 需求分析 | 必备章节齐全 + REQ 编号与 PRD 覆盖对齐（遗漏/臆造即报错） | 错误清单**反馈给模型修正重出** |
| 2 测试方案 | 必备章节（范围/策略/优先级/风险/环境/准入准出/用例量） | 同上 |
| 3 用例生成 | JSON 可解析 + 字段强校验（id 唯一/枚举/非空/P0 必带 @smoke） | 同上 |

```bash
# 默认开启：每阶段最多反馈修正 2 次，最终仍不过 → 整条流水线失败（退出码 1）
prd2tc generate --prd <prd> --examples examples/
# 调重试次数 / 容忍失败 / 完全关闭
prd2tc generate --prd <prd> --max-gate-retries 3
prd2tc generate --prd <prd> --allow-gate-fail     # 仅告警
prd2tc generate --prd <prd> --no-gate             # 退回纯 prompt 自检
```

**三层约束体系**：① prompt 内"输出自检门"（引导模型自检）→ ② 机器门禁校验（程序强制）→ ③ 校验失败反馈重出（修正闭环）。阶段 3 的校验与 `prd2tc validate` 共用同一套规则。

约束模板的"学习约束"部分由 `src/prd2tc/constraints.py` 从检索到的参考示例动态生成（示例库模块/规模、优先级口径、常用标签、推断的设计方法、design_logic 要点），实现「静态原理 + 动态学习」双约束。

**演进方向（规划）**：
- 闭环学习：02 子项目测试执行结果（通过/失败）回写示例库，自动沉淀"验证过的好用例"；
- 语义检索：在零依赖打分之上，可选接向量库（如 sqlite-vec/embedding API）提升相关性；
- 生成后反馈：用例评审意见（Open Items）回流，迭代示例的 design_logic。

## 质量保障

- `prd2tc validate`：用例 JSON 强校验（与 `docs/case-template.md` 一致）。
- `tests/`：14 个单元/端到端测试（含 stub 模式离线跑通全流程）。
- 与配套的 Playwright 测试工程对齐：`check_coverage.py`（AiWebTestCenter/02-Playwright-Automation）校验用例 JSON ↔ Python 测试映射（P0 必达）。

## 开源

- License：MIT
- 零第三方运行时依赖；仅需 Python 3.10+ 与一个 OpenAI 兼容 LLM 端点。
- 目录结构：`src/prd2tc/`（cli/config/llm/loader/stages/validator/io）、`assets/`（内置 prompts/templates）、`tests/`、`skill/`（DSH agent 变体）、`docs/`、`output/`（生成产物）。

## 生态相关项目（AiWebTestCenter）

本工具是 [AiWebTestCenter](https://github.com/Adam-byxiao) 测试体系的一员，独立可发布、也可嵌入整体方案：

- 生成用例 → 配套 Playwright 测试工程（pytest-playwright）按 `TC-*` 编号实现并执行 → CI/CD 跑回归与门禁。
- 用例 JSON 的 `steps[].action/target` 与 `expected` 为「JSON → Python 测试骨架生成器」的输入约定。
- 示例库 `examples/youxuan-mall-examples.json` 由真实项目产出提炼，可作为团队测试设计逻辑的沉淀载体。
