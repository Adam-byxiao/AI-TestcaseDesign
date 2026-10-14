# prd2tc — PRD → 测试方案 → 测试用例

> 把一份产品需求文档（PRD）变成一套**可评审、可执行、可追溯**的测试用例。

`prd2tc` 用大模型完成「需求分析 → 测试方案 → 用例设计」三阶段生成，
产物同时面向两类读者：**人**（Markdown 评审稿、Excel、XMind）与**机器**（结构化用例 JSON，
供 Playwright 等自动化体系消费）。

**技术定位**：Python 3.10+，**运行时零第三方依赖**（仅标准库）。
服务模式与前端工作台为可选依赖，不污染核心 CLI。

---

## 目录

- [核心能力](#核心能力)
- [安装](#安装)
- [快速开始](#快速开始)
- [三条使用路径](#三条使用路径)
- [三阶段流水线](#三阶段流水线)
- [产物结构](#产物结构)
- [关键机制](#关键机制)
- [配置](#配置)
- [文档索引](#文档索引)
- [质量与测试现状](#质量与测试现状)
- [开源](#开源)

---

## 核心能力

| 能力 | 说明 |
|---|---|
| **多模态输入** | `.md` / `.txt` / `.docx` / `.pdf` / `.zip` / 图片；支持**整个文件夹**（含子目录）；零依赖自行解析 OOXML 与 PDF |
| **附件按角色分流** | 需求正文 / 参考资料（接口文档，**不进正文**）/ 原型（**不进正文**，只解析为 UI 契约）/ 图片资产 —— 决定生成质量的上限 |
| **原型 → UI 契约** | 从可运行原型 HTML 提取**真实**的页面键、元素标识符、弹窗按钮与文案，注入生成过程，防止模型编造 |
| **图片视觉识别** | 发现 PRD 引用的图片、按内容关联、交多模态模型识别其中的 UI 事实（字段、状态、弹窗按钮） |
| **三层约束** | prompt 自检门 → 机器门禁 → 失败反馈重出；门禁判据与提示词**同源**，避免"照抄示例却被拦" |
| **确定性设计** | 程序化编号、模块码锚定、需求指纹、优先级重判、冒烟集判定、导出步骤渲染 —— 都可复现 |
| **长文档与并发** | 按标题切分分段抽取；按需求模块并发生成，批次容量按输出预算自适应 |
| **分段执行** | `--stage N` 只跑某阶段、`--from-stage N` 从某阶段跑到最后；上游产物完备性**可校验** |
| **中断恢复** | `--resume` 按输入与参数指纹复用模块检查点；截断与预算耗尽可能**自愈**（批次对半拆分重试） |
| **导出** | Excel（3 sheet）/ XMind（三级导图）/ Markdown 评审稿；支持按**模块 / 优先级 / 标签**（含复合条件）筛选导出 |
| **工作台** | React + antd：三阶段独立操作、批量选择、导出对话框（可选文件名与保存位置）、实时指标与事件 |

---

## 安装

```bash
git clone <本仓库> && cd <仓库目录>
pip install .                      # 或开发模式：pip install -e .
pip install -e '.[service]'        # 可选：服务模式与工作台后端
```

配置 Provider（三选一）：

```bash
# OpenAI 兼容（默认）—— 也支持 DeepSeek/MiniMax 等兼容端点
export PRD2TC_LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-xxx

# Anthropic
export PRD2TC_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-xxx
export PRD2TC_MODEL=claude-sonnet-4-5

# 本地 Ollama（无需 API Key）
export PRD2TC_LLM_PROVIDER=ollama
export PRD2TC_MODEL=qwen3:8b
export PRD2TC_API_BASE=http://localhost:11434
```

> **先自检再生成**：`prd2tc check-llm` 会真实探测一次端点，
> 明确区分「接口地址填错」与「密钥填错」，并给出可用的修正地址。
> 把官网/控制台地址当成 API 地址是常见错误（只会得到一句 HTTP 404）。

---

## 快速开始

```bash
export PRD2TC_LLM_PROVIDER=openai && export OPENAI_API_KEY=sk-xxx

# 1) 全自动三阶段
prd2tc generate --prd examples/sample-prd.md \
  --product sample-mall --version v0.1 --out output

# 2) 不调 API，只看发给模型的 prompt
prd2tc generate --prd examples/sample-prd.md --dry-run

# 3) 离线跑通全流程（内置 stub 响应，无需 API Key）
prd2tc generate --prd examples/sample-prd.md --stub --out /tmp/demo

# 4) 校验 / 统计
prd2tc validate --dir output/sample-mall/v0.1/cases/cases
prd2tc stats    --dir output/sample-mall/v0.1/cases/cases
```

**输入可以更复杂**（`--prd` 可重复，可给目录，可混格式）：

```bash
# 整个文件夹：需求正文 + 图片和附件/ + 原型 HTML，自动还原层级并判定正文
prd2tc generate --prd ./我的PRD/ --product agent --version v0.1

# 显式指定正文，并额外指定图片目录（目录名非常规时）
prd2tc generate --prd PRD.md --prd ./附件/ --images-dir ./截图/ \
  --product agent --version v0.1

# 原型作为 UI 契约真值（通常无需显式指定：文件夹里的 HTML 会被自动识别）
prd2tc generate --prd PRD.md --attach ./prototype.html
```

---

## 三条使用路径

| 路径 | 适用场景 | 入口 |
|---|---|---|
| **CLI** | 线上 / CI / 批量 | `prd2tc`（`src/prd2tc/cli.py`） |
| **服务模式** | 本机工作区 API，供本地工作台连接 | `uvicorn 'prd2tc.service:create_app' --factory` |
| **工作台** | 需要人工审阅、逐步确认、批量筛选 | `cd ui && npm start` |

三者**共用同一个编排内核**（`src/prd2tc/pipeline.py` 的 `PipelineRunner.generate()`）——
CLI 与服务只是它的适配器，因此行为不会漂移。

```bash
# 服务模式
pip install -e '.[service]'
uvicorn 'prd2tc.service:create_app' --factory --host 127.0.0.1 --port 8000

# 工作台（同时起 FastAPI + Vite）
cd ui && npm install && npm start        # 前端 5173 / 后端 8000

# 生产部署
cd ui && npm run build                   # 产物在 ui/dist
# 然后 create_app(ui_dir="ui/dist") 托管静态文件并保留 /api/v1
```

> 工作台默认走**真实模型**：开始运行前必须先在「模型配置」保存 LLM 会话。
> 离线 Stub **不会**自动降级，必须在「新建运行」里显式开启；运行详情会明确标注
> 「真实模型」或「Stub 离线验收」，避免把流程验收产物误认为模型产出。

---

## 三阶段流水线

| 阶段 | 输入 → 输出 | 产物 |
|---|---|---|
| 1 需求分析 | PRD → 结构化需求 | `analysis/需求分析报告.md`、`analysis/requirements.json` |
| 2 测试方案 | 分析报告 → 测试方案 | `plan/测试方案.md` |
| 3 用例设计 | 方案 + 需求 → 用例 | `cases/用例集.md`、`cases/cases/<MODULE>.json`、`cases/traceability.json` |

长文档按标题切分、分段抽取后合并；阶段 3 按需求模块并发生成，每个模块内再按输出预算分批。

**分段执行**（不必每次从头）：

```bash
prd2tc generate --prd PRD.md --stage 1      # 只跑需求分析
prd2tc generate --prd PRD.md --stage 2      # 只跑测试方案（复用已有分析）
prd2tc generate --prd PRD.md --from-stage 3 # 从用例设计跑到最后
prd2tc generate --prd PRD.md --resume       # 复用检查点续跑
```

进入某阶段前会校验上游产物**是否完备**（存在 → 非空 → 可解析 → 结构完整），
并拒绝复用「为另一份 PRD 生成」的陈旧产物：

```
阶段 3（cases）无法开始：上游产物不完备（1 项）
  ✗ [empty] 测试方案（plan/测试方案.md）：产物为空或过小（0B），小于下限 80B
提示：要补跑缺的上游阶段，用 `--from-stage 2`（从这里跑到最后）；
      只跑阶段 3 而不补上游，用 `--stage 3`（会复用已有上游产物）；
      若确认要复用旧产物，加 `--allow-stale-artifacts`。
```

---

## 产物结构

```
output/<product>/<version>/
├── run-manifest.json          # 权威运行清单（runId、哈希、模型参数、阶段用量、状态）
├── manifest.json              # 兼容副本
├── reproduce.json             # --repro 的复现参数
├── analysis/
│   ├── 需求分析报告.md
│   ├── requirements.json      # 结构化需求（含优先级、来源引用、模块表）
│   ├── prd-bundle.json        # 摄取留痕：每个来源的角色与字符数
│   ├── ui-spec.json           # 原型 UI 契约（真实标识符/页面/弹窗/文案）
│   └── image-spec.json        # 图片视觉证据
├── plan/测试方案.md
└── cases/
    ├── 用例集.md               # 评审稿（含自然语言步骤）
    ├── <product>-<version>-用例集.xlsx / .xmind  # 可分发产物
    ├── traceability.json      # 需求 ↔ 用例映射与覆盖状态
    ├── cases/<MODULE>.json    # 机读用例（结构化步骤，供自动化消费）
    ├── .checkpoints/          # 模块级检查点（--resume 复用）
    └── partial/               # 门禁未通过时的部分产物（附 READ-ME，明确不可直接采信）
```

---

## 关键机制

### 输入侧：附件按角色分流

一份 PRD 常同时带来「需求正文」「接口文档」「可运行原型」「截图」。
它们对生成的价值完全不同，**混在一起是生成质量崩坏的首要原因**：

> 实测一份 3,993 字符的真实 PRD，因把 42KB 原型源码与 27KB 接口文档一并内联，
> 喂入模型 **73,159 字符（94.5% 与需求无关）**，产出 **319 条需求 / 43 个模块**，
> 其中甚至包含接口文档里某个**客户专用策略开关**（与被测项目毫无关系）。
> 按角色分流后正文回落到 3,993 字符（↓95%）。

因此摄取层把来源分为：需求载体 / **参考资料**（默认不进正文）/ **原型**（不进正文，
只解析为 UI 契约）/ 资产。只找到参考资料时会**明确报错**，而不是静默当正文。

→ 详见 [docs/ingestion.md](docs/ingestion.md)

### 契约真值：原型与图片

真实 PRD 常引用可运行原型，而**页面的真实标识符只存在于原型里**。
不使用它时，模型会编造（实测 143/143 个标识符不存在、7/7 个 URL 路由不存在、
并按臆造的「三选项弹窗」设计交互，而真值只有 1 个按钮）。

`prd2tc` 从原型提取结构化 UI 契约并注入 system 前缀，门禁**按种类**校验用例引用：
`id=` 只对元素 id、`class=` 只对 class 锚点、`page=`/`tab=` 只对页面键与页签键
（把页签键当元素 id 用会被拒绝）。

→ 详见 [docs/ingestion.md](docs/ingestion.md)

### 三层约束体系

| 层 | 机制 | 能力边界 |
|---|---|---|
| ① prompt 自检门 | 提示词内置「输出前自检清单」 | 靠模型自觉 |
| ② 机器门禁 | `gates.py` + `validator.py` 程序校验 | **强约束** |
| ③ 反馈重出 | 门禁错误回灌给模型修正（可配重试次数） | 修正闭环 |

门禁判据与提示词示例**必须一致**，否则模型会陷入「照抄示例 → 被门禁拦 → 改写」的死循环；
项目用测试锁住这种一致性。修复提示词的范围也必须与门禁允许集合一致 ——
否则重试会变成死循环（实测曾因此整模块失败）。

→ 详见 [docs/prompts-and-gates.md](docs/prompts-and-gates.md)

### 确定性与可复现

编号、模块码、优先级、冒烟集、导出步骤都由**程序**判定而非模型自由发挥：

- **用例编号**程序化重排，模块码锚定到 PRD 原始编号
- **优先级** = 需求优先级 × 测试维度修正（含「写操作防护不降级」的数据安全例外）
- **`@smoke`** 是 P0 的**真子集**（每需求 1 条、排除边界与异常、总量 ≤20%）
- **导出步骤**用确定性渲染成自然语言，JSON 仍保留结构化步骤

```bash
prd2tc generate --prd PRD.md --repro        # 温度归零，作为回归验收基线
prd2tc compare-runs --a output/p/v0.1 --b output/p/v0.2   # 结构化对拍
```

→ 详见 [docs/case-spec.md](docs/case-spec.md)、[docs/pipeline.md](docs/pipeline.md)

### 导出与筛选

`generate` 成功后**自动**产出 Excel / XMind，无需额外操作。也可手动导出并**按范围筛选**：

```bash
prd2tc export --cases output/mall/v0.3/cases/cases --format xmind --xmind-mode full

# 工作台/服务侧还支持按条件筛选导出：
#   只导出某模块、只导出 P0、只导出 @smoke，或复合条件（如「KB 模块中的回归用例」）
```

| 产物 | 内容 |
|---|---|
| **`.xlsx`** | 3 个 sheet：`用例明细`（条件着色、冻结表头、自动筛选）、`需求覆盖`、`统计概览` |
| **`.xmind`** | 模块 → 子模块 → 用例 三级导图；优先级为图标，完整详情在节点备注 |
| **`.md`** | 评审稿：统计 + 追溯表 + 逐条用例（**自然语言步骤**） |

两种二进制格式都由**标准库**直接生成（ZIP + XML/JSON 子集），
固定时间戳与稳定节点 id，因此**同输入产出字节一致**；导出失败**不阻断**生成主流程。

→ 详见 [docs/export.md](docs/export.md)

---

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRD2TC_LLM_PROVIDER` | `openai` | `openai` / `anthropic` / `ollama` |
| `PRD2TC_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | - | Provider 凭证；Ollama 不需要 |
| `PRD2TC_API_BASE` | Provider 默认 | 可覆盖 API 地址（**注意别填官网地址**） |
| `PRD2TC_MODEL` | 随 Provider | 模型名 |
| `PRD2TC_MAX_TOKENS` | `16384` | 单次输出**上限**。按实际输出计费，调高不增加固定成本。不得超过 Provider 的模型输出上限 |
| `PRD2TC_TOKENS_PER_REQ` | `1800` | 批次容量估算：每条需求预期输出 token（实测校准） |
| `PRD2TC_CASE_BATCH_CAP` | `8` | 单批需求数上限，防止批次随预算膨胀 |
| `PRD2TC_CASE_OUTPUT_BUDGET` | `0.60` | 单批允许占用 `max_tokens` 的比例。**调高它会让 `MAX_TOKENS` 失效**（批次同比例变大，安全余量恒为 `1/该值`） |
| `PRD2TC_TEMP_S1/S2/S3` | `0.2/0.3/0.3` | 分阶段温度；`PRD2TC_TEMPERATURE` 可统一覆盖 |
| `PRD2TC_TIMEOUT` | `120` | 请求超时（秒） |
| `PRD2TC_MAX_RETRIES` | `3` | 网络失败重试次数（指数退避） |
| `PRD2TC_STREAM` | `1` | 流式输出（实时进度与 Token）；`--repro` 强制关闭 |
| `PRD2TC_STREAM_PUSH_MS` | `500` | 实时指标推送间隔（毫秒） |
| `PRD2TC_JOBS` | `1` | 默认并发批次数（工作台默认 4）。实测串行 5 模块约 1013s，并发约 332s；过高会触发 Provider 限流 |
| `PRD2TC_MARKDOWN_TRANSIT` | `0` | 阶段 3 改输出 Markdown 表格再由程序转 JSON，规避转义类失败（字符约省 50%，落盘仍为 JSON） |
| `PRD2TC_PROMPT_CACHE` | `0` | Anthropic 请求带 `cache_control` |
| `PRD2TC_REPRO` | `0` | 可复现模式（三阶段温度归零） |
| `PRD2TC_SEED` | - | 采样种子（仅 OpenAI 兼容端点生效） |
| `PRD2TC_CHUNK_SIZE` / `_OVERLAP` | `6000` / `1200` | 长文档切分参数 |
| `PRD2TC_EXAMPLES` | - | 示例库目录（学习能力，见下） |

**批次容量公式**（当你想调大单批规模时读这一条）：

```
每批需求数 = min(max_tokens × OUTPUT_BUDGET_RATIO ÷ TOKENS_PER_REQ_EST,
                 CASE_BATCH_CAP,
                 模块需求数)
```

### 学习能力（示例库，RAG-lite）

让模型从**已有的 PRD-用例对**中学习测试设计逻辑，而不是每次从零设计：

```bash
prd2tc generate --prd <prd> --examples examples/ --examples-k 3
```

示例文件的 `design_logic` 字段（为什么这么设计：P0 必带 `@smoke`、反向断言、金额边界…）
是模型学习的关键。相关性检索用零依赖的轻量打分（模块命中 + 关键词覆盖 + 需求要点重合）。

---

## 文档索引

| 文档 | 内容 | 读者 |
|---|---|---|
| [architecture.md](docs/architecture.md) | 总体架构、模块地图、数据流、关键设计决策 | 想理解全局的人 |
| [pipeline.md](docs/pipeline.md) | 三阶段细节、批次与去重、产物契约、分段执行、恢复与可复现 | 要改流水线的人 |
| [ingestion.md](docs/ingestion.md) | 多格式摄取、文件夹、附件分流、原型契约、图片视觉 | 处理输入问题的人 |
| [case-spec.md](docs/case-spec.md) | 用例字段与规则契约、动作词表、优先级、冒烟集、追踪矩阵 | 消费/生成用例的人 |
| [prompts-and-gates.md](docs/prompts-and-gates.md) | Prompt 体系、三层约束、门禁清单、示例库 | 调提示词与门禁的人 |
| [export.md](docs/export.md) | Excel/XMind/Markdown 导出、筛选导出、自然语言步骤 | 要分发产物的人 |
| [service-and-ui.md](docs/service-and-ui.md) | 服务 API、会话模型、工作台交互与前端设计取舍 | 用/改工作台的人 |
| [operations.md](docs/operations.md) | 配置、运行、排障手册（含常见错误与处置） | 运维与排障 |
| [development.md](docs/development.md) | 开发约定、测试方法论（TDD + 变异测试）、平台注意事项 | 贡献代码的人 |
| [quality-evaluation.md](docs/quality-evaluation.md) | 用例质量评测：方法、评分、未验证风险 | 想了解产出质量的人 |

提示词资产在 `prompts/`，模板在 `templates/`。
**注意**：`src/prd2tc/assets/prompts/` 是安装包的兜底副本，
改动提示词后必须运行 `python scripts/sync_prompts.py` 同步（有测试校验一致性）。

---

## 质量与测试现状

| 项 | 现状 |
|---|---|
| 后端测试 | 当前结果以本机运行 `python -m pytest tests -q -p no:cacheprovider` 为准 |
| 前端测试 | Vitest 测试位于 `ui/`；执行 `npm test` |
| 前端构建 | 执行 `npm run build`，TypeScript 检查与 Vite 生产构建均需通过 |
| 用例质量评测 | `quality-evaluation.md` 中的数据来自特定样本，不能视为所有 PRD 的质量保证 |

测试方法论的两条硬要求（详见 [development.md](docs/development.md)）：

1. **先写复现测试再修**（TDD）。例如分页定位缺陷修复前新增的测试 6/6 失败，修复后 6/6 通过。
2. **变异测试**：故意破坏实现，确认测试能捕获。这条做法在本项目反复发现「改了代码但没写测试」
   与「测试无从区分」两类问题。

> **未验证项（切勿假定可用）**：真实多模态视觉识别质量、真实 docx/PDF 语料、
> 用例真实驱动 UI 的可执行性 —— 详见 [quality-evaluation.md](docs/quality-evaluation.md)。

---

## 开源

- **License**：MIT
- **依赖**：核心 CLI 零第三方运行时依赖；服务端需安装 `.[service]`，前端需 Node.js/npm；真实生成需可访问的模型端点
- **目录结构**：

  ```
  src/prd2tc/           核心包（CLI / 编排 / 摄取 / 门禁 / 导出 / 服务）
  src/prd2tc/assets/    安装包内置的 prompts 与 templates 副本
  prompts/  templates/  提示词与模板（仓库内优先）
  tests/                后端测试
  ui/                   React + antd 工作台（含前端测试）
  examples/             示例 PRD 与示例库
  docs/                 设计与使用文档
  ```

### 生态相关项目（AiWebTestCenter）

本工具是 AiWebTestCenter 测试体系的一员，**独立可发布**，也可嵌入整体方案：

- 生成用例 → 配套 Playwright 测试工程按 `TC-*` 编号实现并执行 → CI/CD 跑回归与门禁。
- 用例 JSON 的 `steps[].action/target` 与 `expected` 是「JSON → Python 测试骨架生成器」的输入约定。
- `examples/youxuan-mall-examples.json` 由真实项目产出提炼，可作为团队测试设计逻辑的沉淀载体。
