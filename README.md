# 01-AI-TestCaseDesign / prd2tc

> 远程协作开发者请先阅读 [当前开发交接说明](docs/developer-handoff.md)，其中包含实现状态、架构/API、运行测试方式、已知限制和下一步优先级。

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
git clone <本仓库> && cd <仓库目录>
pip install .            # 或开发模式 pip install -e .
export PRD2TC_LLM_PROVIDER=openai  # openai / anthropic / ollama
export OPENAI_API_KEY=sk-xxx      # openai 必填；ollama 不需要
```

## 快速开始

```bash
# 在仓库目录（含 examples/）内执行；pip install . 后也可在任何目录使用
export PRD2TC_LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-xxx

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
| `PRD2TC_LLM_PROVIDER` | `openai` | `openai` / `anthropic` / `ollama` |
| `PRD2TC_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | - | Provider 凭证；Ollama 不需要 |
| `PRD2TC_API_BASE` | Provider 默认地址 | 可覆盖 OpenAI、Anthropic 或 Ollama 地址 |
| `PRD2TC_MODEL` | 随 Provider 选择 | 模型名 |
| `PRD2TC_MAX_TOKENS` | `16384` | 单次输出上限。**只是上限，按实际输出计费，调高不增加固定成本**；实测用例约 1700 tok/需求，个别模块 3 条需求即超过 8192。不得超过 Provider 的模型输出上限（Anthropic 尤甚） |
| `PRD2TC_TOKENS_PER_REQ` | `1800` | 批次容量估算：每条需求预期输出 token（实测校准值） |
| `PRD2TC_CASE_BATCH_CAP` | `8` | 单批需求数上限，防止批次随预算膨胀 |
| `PRD2TC_CASE_OUTPUT_BUDGET` | `0.60` | 单批允许占用 `max_tokens` 的比例。**调高它会让 `PRD2TC_MAX_TOKENS` 失效**：批次会同比例变大，安全余量恒为 `1/该值`，截断率不变 |
| `PRD2TC_TEMPERATURE` | `0.4` | 采样温度 |
| `PRD2TC_TIMEOUT` | `120` | 请求超时（秒） |
| `PRD2TC_MAX_RETRIES` | `3` | 失败重试次数（指数退避） |
| `PRD2TC_STREAM` | `1` | 流式输出（实时进度/Token）；`0` 关闭。`--repro` 时强制关闭 |
| `PRD2TC_STREAM_PUSH_MS` | `500` | 实时指标推送间隔（毫秒），限流用 |
| `PRD2TC_JOBS` | `1` | 默认并发批次数（阶段1分段 / 阶段3模块并行）。工作台默认 4；实测串行 5 模块约 1013s，并发约 332s |
| `PRD2TC_MARKDOWN_TRANSIT` | `0` | 阶段3 改输出 Markdown 表格再由程序转 JSON（`--markdown-transit`）。规避免嵌套转义导致的整轮失败，字符约省 50% |

> **配置自检**：CLI 用 `prd2tc check-llm`；工作台在「模型配置」保存前会真实探测一次端点，
> 不通过则拒绝保存并给出具体原因（区分「接口地址填错」与「密钥填错」）与可用的修正地址，
> 避免错误配置到运行阶段才以无信息量的形式暴露（例如把官网地址当成 API 地址，只会得到一句 HTTP 404）。
>
> **实时进度**：默认开启流式。运行期间可在工作台看到当前阶段/模块/批次、实时 Token（估算值标 `≈`）、
> 首 Token 延迟与**静默时长**（判断「是否卡住」的直接证据），并可随时取消（流式会立即停止接收，不再计费）。
> 流式实时推送依赖 WebSocket，故 `pip install '.[service]'` 会一并安装 `websockets`；
> 缺少它时 uvicorn 无法完成 WebSocket 升级，实时推送会静默退化为 10 秒轮询。

Provider 示例：

```bash
# Anthropic
export PRD2TC_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-xxx
export PRD2TC_MODEL=claude-sonnet-4-5

# 本地 Ollama（无需 API Key）
export PRD2TC_LLM_PROVIDER=ollama
export PRD2TC_MODEL=qwen3:8b
export PRD2TC_API_BASE=http://localhost:11434
```

## 三阶段 Pipeline

| 阶段 | Prompt | 输入 → 输出 |
| --- | --- | --- |
| 1 需求分析 | `prompts/01-prd-analysis.md` | PRD → `analysis/需求分析报告.md` |
| 2 测试方案 | `prompts/02-test-plan.md` + 模板 | 分析报告 → `plan/测试方案.md` |
| 3 用例生成 | `prompts/03-test-cases.md` + 字段约束 | 测试方案 → `cases/用例集.md` + `cases/cases/<MODULE>.json` |

产物目录：`output/<product>/<version>/{analysis,plan,cases}/`（含权威 `run-manifest.json` 与兼容副本 `manifest.json`）。

当前版本还会额外输出 `analysis/requirements.json` 和 `cases/traceability.json`，并在 `run-manifest.json` 中记录运行 ID、PRD 哈希、模型参数、阶段用量和最终状态。为兼容旧工具会同步写出 `manifest.json`；旧版顶层数组用例 JSON 仍可读取。

需求版本可通过以下命令生成确定性 Diff：

```bash
prd2tc diff --prd PRD-v0.4.md --from output/product/v0.3 --out output
```

Python 调用方可使用 `prd2tc.pipeline.PipelineRunner`，执行结果契约见 `prd2tc.execution`。

### 产物导出（Excel / XMind）

`generate` 成功后会**自动**在 `cases/` 下产出两种可分发格式，无需额外操作：

```bash
# 自动：generate 结束即产出 <product>-<version>-用例集.xlsx / .xmind
prd2tc generate --prd examples/sample-prd.md --product mall --version v0.3 --stub

# 手动导出（对已有产物，可指定格式与展开粒度）
prd2tc export --cases output/mall/v0.3/cases/cases                     # 两种格式
prd2tc export --cases output/mall/v0.3/cases/cases --format excel
prd2tc export --cases output/mall/v0.3/cases/cases --format xmind --xmind-mode full
```

| 导出 | 内容 |
| --- | --- |
| **`.xlsx`** | 3 个 sheet：`用例明细`（13 列，P0/P1 条件着色、冻结表头、自动筛选）、`需求覆盖`（REQ↔TC 映射，覆盖不足/未覆盖标红）、`统计概览` |
| **`.xmind`** | 模块 → 子模块 → 用例 三级导图；优先级渲染为图标，标签与设计方法为文本标签，完整用例详情在节点备注中 |

两种格式都**用标准库直接生成**（ZIP + XML/JSON 子集），未引入 openpyxl 等依赖，保持「零第三方运行时依赖」；产物已用 openpyxl 交叉验证可被严格解析器读取。ZIP 时间戳与节点 id 均确定性生成，因此**同输入产出字节一致**，可参与 `compare-runs` 对拍。

导出失败**不会**阻断生成流程，只降级为告警（产物其余部分照常落盘）。
服务模式下工作台「运行详情」提供导出按钮，接口为 `GET /api/v1/exports/{run_id}/{excel|xmind}`。

> 设计细节、字段映射与实现取舍见 [docs/export-excel-xmind.md](docs/export-excel-xmind.md)。

长文档会按 Markdown 标题/段落切分，阶段 1 分段抽取后统一合并，阶段 3 按需求模块并发生成。可用 `--jobs 4` 控制并发；中断后用 `--resume` 复用输入、Prompt、模型和参数指纹一致的模块检查点，`--force` 明确放弃旧检查点。

增量用例和执行反馈命令：

```bash
prd2tc merge --from output/product/v0.3 --new output/product/v0.4 \
  --diff output/product/v0.4/diff/requirement-diff.json --out output/product/v0.4/merged
prd2tc import-results --cases output/product/v0.4/cases/cases \
  --results execution-results.json --out output/product/v0.4/cases-with-results
prd2tc promote-examples --cases output/product/v0.4/cases-with-results \
  --design-logic "人工评审后的设计逻辑" --reviewed --out examples/product-v0.4.json
```

可选服务模式不会增加核心 CLI 的安装依赖：

```bash
pip install '.[service]'
uvicorn 'prd2tc.service:create_app' --factory --host 127.0.0.1 --port 8000
```

### 可交互前端工作台

前端位于 `ui/`，开发时分别启动 API 和 Vite：

```bash
# 安装一次服务端可选依赖
pip install -e ".[service]"
# 一个命令同时启动 FastAPI + Vite
cd ui && npm install && npm start
```

`npm start` 是 `npm run dev:all` 的别名，会同时启动前端（5173）和后端（8000）；也可以分别使用 `npm run dev` 或 `npm run dev:backend`。

浏览器访问 <http://127.0.0.1:5173>。工作台支持 PRD 版本管理、Markdown 预览、需求/方案/用例阶段草稿与显式审批、实时事件、追踪矩阵和产物下载。Provider 会话在“模型配置”页面创建；API Key 只保留在服务进程内存中，Ollama 不需要 Key。

工作台默认按正式流程使用真实模型：开始运行或点击“重新分析”前，必须先在“模型配置”保存当前 LLM 会话。离线 Stub 不会自动降级，只有在“新建运行”中显式打开“Stub”开关才会使用固定离线响应；运行详情会明确显示“真实模型”或“Stub 离线验收”，避免把流程验收产物误认为模型生成结果。

生产部署可先执行 `cd ui && npm run build`，再将 `ui/dist` 传给 `create_app(ui_dir="ui/dist")`；服务会托管静态文件并保留 `/api/v1` 接口。

服务提供 `POST /runs`、`GET /runs/{task_id}` 和 WebSocket `/runs/{task_id}/events`；任务状态持久化到本地状态目录，WebSocket 不是唯一事实源。
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

遇到门禁错误时，请先按错误类型判断是 PRD 事实缺失、模型输出不完整、数据契约错误还是 Provider/环境问题；不要默认修改 PRD。详细的判断标准、UI/CLI 操作和提交问题所需信息见 [门禁失败处理手册](docs/gate-troubleshooting.md)。

约束模板的"学习约束"部分由 `src/prd2tc/constraints.py` 从检索到的参考示例动态生成（示例库模块/规模、优先级口径、常用标签、推断的设计方法、design_logic 要点），实现「静态原理 + 动态学习」双约束。

**演进方向（规划）**：
- 闭环学习：02 子项目测试执行结果（通过/失败）回写示例库，自动沉淀"验证过的好用例"；
- 语义检索：在零依赖打分之上，可选接向量库（如 sqlite-vec/embedding API）提升相关性；
- 生成后反馈：用例评审意见（Open Items）回流，迭代示例的 design_logic。

## 质量保障

- `prd2tc validate`：用例 JSON 强校验（与 `docs/case-template.md` 一致）。
- `tests/`：14 个单元/端到端测试（含 stub 模式离线跑通全流程）。
- 与配套的 Playwright 测试工程对齐：`check_coverage.py`（AiWebTestCenter/02-Playwright-Automation）校验用例 JSON ↔ Python 测试映射（P0 必达）。

### 输出健壮性（针对推理型模型）

实测 MiniMax-M3 在这套流程上有三个坑，均已针对性处理（`PRD2TC_*` 可调）：

1. **`testData` 禁止嵌套**。契约收紧为「键=值」字符串数组（`["phone=13800138000"]`），
   值只能是标量。嵌套对象/数组需要多层引号转义，实测导致模型在思考里反复自我纠错、
   整轮输出归零。旧版 object 产物**仍可读取**（自动归一化）。
2. **思考与产出分离**。M3 会把推理写进 `content` 的 `<think>`（部分端点走独立
   `reasoning_content`），两者都会被单独统计：实时指标区分「思考中 / 产出中」，
   `extract_cases` 优先从**剥离思考后的正文**提取，避免把思考里的示例 JSON 当产物。
3. **预算耗尽自愈**。若输出被截断且正文为空、思考占满预算，系统会给出可行动诊断，
   并把该批次**对半拆分重试**（最多 2 层）——原地重试同样大的批次几乎必然再次失败。

`--markdown-transit`（或 `PRD2TC_MARKDOWN_TRANSIT=1`）可让阶段 3 改为输出 Markdown 表格、
再由程序转成 JSON：表格没有引号嵌套，转义类失败从根上消失；实测同一内容字符数约省 50%，
**落盘产物仍是 JSON，下游契约不变**。

## 开源

- License：MIT
- 零第三方运行时依赖；仅需 Python 3.10+ 与一个 OpenAI 兼容 LLM 端点。
- 目录结构：`src/prd2tc/`（cli/config/llm/loader/stages/validator/io）、`assets/`（内置 prompts/templates）、`tests/`、`skill/`（DSH agent 变体）、`docs/`、`output/`（生成产物）。

## 生态相关项目（AiWebTestCenter）

本工具是 [AiWebTestCenter](https://github.com/Adam-byxiao) 测试体系的一员，独立可发布、也可嵌入整体方案：

- 生成用例 → 配套 Playwright 测试工程（pytest-playwright）按 `TC-*` 编号实现并执行 → CI/CD 跑回归与门禁。
- 用例 JSON 的 `steps[].action/target` 与 `expected` 为「JSON → Python 测试骨架生成器」的输入约定。
- 示例库 `examples/youxuan-mall-examples.json` 由真实项目产出提炼，可作为团队测试设计逻辑的沉淀载体。
