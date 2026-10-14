# prd2tc 开发与测试约定

> 本文的每一条约定都来自本仓库真实踩过的坑，来源标注到文件/提交。
> 目标读者：在本仓库改代码、加能力、写测试的人。

## 1. 环境准备与运行

### 1.1 后端

```bash
# 只跑 CLI（运行时零第三方依赖）
pip install -e .

# 需要服务模式 / 工作台后端（fastapi + uvicorn + websockets + python-multipart）
pip install -e '.[service]'

# 全量后端测试
python -m pytest tests -q

# 单个文件 / 单个用例
python -m pytest tests/test_stage_resume.py -q
python -m pytest tests/test_gates.py::test_missing_section_is_reported -q
```

`pyproject.toml` 要点：

- `requires-python = ">=3.10"`（用到了 `X | Y` 类型语法与 `dict[str, ...]`）。
- 运行时**零第三方依赖**：`dependencies` 为空，HTTP 走标准库 `urllib.request`（`llm.py:45-108`），连 Excel/XMind 导出都是自己拼 ZIP + XML（`export/common.py`）。**加依赖前请先确认它不能改用标准库实现**。
- `[project.optional-dependencies].service` 里的 `websockets` 是**必需**而非可选：缺它时 uvicorn 无法完成 WebSocket 升级，实时推送会静默退化为 10 秒轮询（`pyproject.toml:24-25`）。
- `[tool.pytest.ini_options] testpaths = ["tests"]`。需要 fastapi 的用例统一用 `pytest.importorskip("fastapi")` 自我保护（`tests/test_service_api.py:7`、`tests/test_realtime.py:8`），因此**未装 service extra 时后端测试仍能整体跑通**（跳过相关文件）。
- 测试不读仓库外的 PRD：`tests/test_examples.py:12-13` 记录过"旧版指向仓库外 `PRD_File/`，导致新克隆者必失败"，现在端到端 fixture 全部用仓库内 `examples/sample-prd.md`。

> 测试数量会随回归用例变化；不要把旧执行记录当作当前基线。以当前分支执行 `python -m pytest tests -q -p no:cacheprovider` 的结果为准。

### 1.2 前端

```bash
cd ui
npm install
npm test          # 等价 npx vitest run
npm run build     # tsc -b && vite build → ui/dist
```

`ui/package.json` 的脚本：

| 脚本 | 命令 | 用途 |
| --- | --- | --- |
| `dev` | `vite` | 只起前端（5173） |
| `dev:backend` | `python -m uvicorn prd2tc.service:create_app --factory --host 127.0.0.1 --port 8000` | 只起后端 |
| `dev:all` / `start` | `node scripts/dev-all.mjs` | 同时起前后端 |
| `build` | `tsc -b && vite build` | 生产构建到 `ui/dist` |
| `test` | `vitest run` | 跑 `ui/src/test/*.test.tsx` |

`ui/vitest.config.ts` 的两个非默认设置都有理由：

- `environment: 'jsdom'` + `setupFiles: ['./src/test/setup.ts']`（提供 jest-dom 匹配器与 ResizeObserver 兜底）。
- `testTimeout: 20000` / `hookTimeout: 20000`（默认 5s 的坑见 §4.5）。

前端测试文件与用例数会随改动变化；运行 `npm test` 获取当前结果。现有覆盖包括：`StageEditorPaging.test.tsx`(6) 分页后「详情/编辑」必须定位到被点的那一条（数据损坏级 bug，见 §3.1）；
`StageEditor.test.tsx`(4) 后台轮询不得覆盖正在编辑的内容；`StageSteps.test.tsx`(13) 三阶段独立步骤的状态流转；
`StageStepsArtifact.test.tsx`(8) 「查看产物」的受控展开与滚动定位；`CaseFilterPanel.test.tsx`(13) 三维度筛选、实时数量、禁用但仍展示可选项；
`ExportDialog.test.tsx`(20) 导出范围提示、文件名片段、按筛选导出；`PrdPage.test.tsx`(8) PRD 上传（含 antd 逐文件回调去重）与重新分析；
`RunProgress.test.tsx`(2) 运行进度与实时事件渲染。

## 2. 测试组织（33 个后端测试文件）

`tests/` 下**没有子目录**，全部按主题平铺。下表的分组是本仓库的实际逻辑分组；文件名逐字来自目录列表：

### 2.1 门禁与校验契约（4）

`test_gates.py` —— 三阶段必备章节校验、REQ 覆盖双向对齐（遗漏与臆造都报错）、"门禁失败 → 反馈重出"闭环（`gates.py`）。
`test_gate_constraints.py` —— A5/A6/A7 专项：**门禁不得拦掉 prompt 自带的示例**，否则模型陷入「照抄示例 → 被门禁拦 → 改写」死循环。
`test_validator.py` —— 单条用例字段校验：id 格式/唯一、枚举、非空、`design_method`。
`test_test_data_contract.py` —— `testData` 必须是「键=值」字符串数组；嵌套对象/数组需要多层引号转义，是 JSON 写坏的首要来源；
**旧版 object 产物仍需可读**（自动归一化）。

### 2.2 提示词工程与约束注入（3）

`test_prompt_engineering.py` —— Prompt 工程十原则的回归锁定：分隔符 / 近因（输出要求置末）/ 角色 / 温度 / 少样本 / 负向约束。
`test_constraints.py` —— 学习模式提取、模板填充、三阶段注入（`constraints.py`）。
`test_actions.py` —— 动作词表单一事实源：分组、每动作一句话说明、`render_actions_for_prompt()` 与提示词文本同源。

### 2.3 用例契约、去重与自愈（5）

`test_cross_batch_dedup.py` —— 跨批次重复生成（实测运行 `c653dc92`：KB 16 条需求切成 2 批，两批都覆盖全部 16 条 → 52% 冗余）；
**接线回归**：修复提示词必须只声明"本批"编号。
`test_requirement_dedup.py` —— 需求语义去重（`_dedupe_semantic`）：同义改写要合并，但**不得误并** —— 曾试图把
`rerank_score > 0.2` 与 `> 0.39` 合并（数值冲突是两条不同规则）；含**接线**：`requirements_from_analysis` 必须真的调用去重。
`test_budget_healing.py` —— 截断处理与自愈：推理耗尽（26,504 字符产出 0 字正文）与静默截断（运行 `9d97060e` 的合法但残缺数组）。
`test_reasoning.py` —— `<think>` 分离、跨块标记、未闭合思考，以及"预算被思考耗尽"的识别（`reasoning.py`）。
`test_markdown_transit.py` —— 表格 ⇄ JSON 往返保真、竖线/换行中和、与 JSON 的体积对比、**完全不产生转义引号**。

### 2.4 需求语义、优先级与冒烟集（3）

`test_priority.py` —— 优先级 = 需求优先级 × 测试维度修正 + 数据安全例外；`regrade` 批量重判的确定性。
`test_smoke_set.py` —— `@smoke` 必须是 P0 的**真子集**：排除边界/异常维度、每需求最多 1 条、总量 `min(25, 20%×总数)`。
背景实测：v2 冒烟集占全量 **74%**，那已经不叫冒烟。
`test_long_doc.py` —— 长文档程序化渲染 + 多分段 stub 端到端（防输出截断的架构性保障）。

### 2.5 摄取与多模态（2）

`test_ingest.py` —— 多格式（md/txt/docx/pdf/zip/图片）、多文件、文件夹层级（depth）归一化。
用**自包含合成样本**（最小 docx/zip/pdf），不依赖外部文件。
`test_images.py` —— 图片发现 → 关联（含用户实际提供的目录）→ 视觉分析（打桩）→ 渲染 → 降级。

### 2.6 阶段产物、分段执行与原型契约（6）

`test_stage_resume.py` —— 产物完备性五类问题（missing/empty/malformed/incomplete/stale）与 `--stage N` / `--from-stage N` 的语义区分。
`test_pipeline_stub.py` —— 离线端到端：stub 跑通三阶段 + 校验通过 + manifest 契约。含**接线回归**：流水线必须真的调用 `priority.regrade`。
`test_new_core.py` —— 内核单测合集：分块与定位、追溯矩阵、diff/执行契约、需求 id 稳定性、模块检查点恢复、动态模块校验、增量用例状态机、`cli.generate` 只是 `PipelineRunner` 的薄适配器、`partial/` 保留。
`test_prototype.py` —— 原型 UI 契约抽取/渲染/按契约校验用例；含**接线回归**：`generate()` 必须把自动识别的原型真正接进阶段 3 并落盘 `ui-spec.json`。
`test_examples.py` —— 示例库加载/检索/渲染注入/端到端。
`test_reproduce.py` —— 结构指纹对文本变化不敏感；`compare_runs` 对同源漂移/异源分类正确。

### 2.7 服务 API 与实时（3）

`test_service_api.py` —— 端点契约、产物下载与安全（`_safe_output` / `_download_name` 响应头注入防护 / 上传上限）、会话过期 401、草稿与审批、`_credential_error` 纯函数。也含**接线回归**：旧版 `POST /runs` 不传 `sessionId` 的路径。
`test_realtime.py` —— 结构化进度事件、心跳、`GET /runs/{id}` 内嵌 metrics、`?metrics=1` 的 opt-in 兼容护栏、终态仍推最后一帧、取消落 `cancelled`、`--repro` 强制非流式。
`test_llm_stream.py` —— 三家 Provider 的 SSE/NDJSON 协议（全部离线录制片段）、usage 缺失时的估算标记、首 token 前可重试/后不可重试、取消立即生效、推理字段绝不混入 `content`。

### 2.8 工程护栏与工具（5）

`test_concurrency_defaults.py` —— `PRD2TC_JOBS` 生效且非法值回落 1；CLI `--jobs` 默认留空交由 Config 决定；服务体缺省时取 Config；显式值覆盖环境变量。
`test_casefilter.py` —— 筛选语义「维度内 OR、跨维度 AND」、分面统计 `ignore`、确定性。
`test_export.py` —— Excel/XMind 导出的可执行版规范（对应 `docs/export.md` 中的导出契约）。
`test_nl_steps.py` —— 自然语言步骤渲染：**JSON 保留结构化**（门禁与自动化驱动用），**导出物用自然语言**（给人评审）。
`test_llm_probe.py` —— 配置自检与错误透传：apiBase 填成官网地址 → 404 返回 HTML；`ValidationError` 只暴露「N 个校验问题」会丢失真实原因。
`test_llm_providers.py` —— 三家 Provider 的**非流式**请求体与响应形状（即 `--repro` 走的路径）。

### 2.9 一张表：文件 → 主题

`test_actions.py` 动作词表 · `test_budget_healing.py` 预算自愈 · `test_casefilter.py` 筛选 · `test_concurrency_defaults.py` 并发默认值 · `test_constraints.py` 约束注入 · `test_cross_batch_dedup.py` 跨批去重 · `test_examples.py` 示例库 · `test_export.py` 导出 · `test_gate_constraints.py` 门禁约束 · `test_gates.py` 门禁 · `test_images.py` 图片 · `test_ingest.py` 摄取与多格式 · `test_llm_probe.py` 配置自检 · `test_llm_providers.py` Provider 契约 · `test_llm_stream.py` 流式 · `test_long_doc.py` 长文档 · `test_markdown_transit.py` 中转 · `test_metrics.py` 实时指标 · `test_new_core.py` 内核合集 · `test_nl_steps.py` 自然语言步骤 · `test_pipeline_stub.py` 冒烟集（端到端） · `test_priority.py` 优先级 · `test_prompt_engineering.py` 提示词工程 · `test_prototype.py` 原型契约 · `test_realtime.py` 实时与流式 · `test_reasoning.py` 思考分离 · `test_reproduce.py` 可复现 · `test_requirement_dedup.py` 需求语义去重 · `test_service_api.py` 服务 API · `test_smoke_set.py` 冒烟集 · `test_stage_resume.py` 产物完备性与分段执行 · `test_test_data_contract.py` 数据契约 · `test_validator.py` 字段校验。

## 3. 本项目的测试方法论

### 3.1 TDD：先写**能精确复现缺陷**的测试，再修实现

顺序固定为三步：**复现测试（先红）→ 修实现 → 确认转绿**。

本项目最有说服力的例子是 UI 分页定位 bug（提交 `fd87899`）：

- **现象**：用例生成完成后，在第 2 页点「详情」只会看到第 1 页同位置的用例；在第 2 页改标题/状态会被**静默**作用到第 1 页那一条。
- **根因**：antd `Table` 的 `render(value, record, index)` 里 `index` 是**当前页内序号**，不是全局下标；同一个错误也用在 `change(i, field, value)` 上，因此不是显示瑕疵而是**数据损坏**。
- **复现测试**：新增 `ui/src/test/StageEditorPaging.test.tsx`（6 个用例，15 条数据必然分页），断言"只影响被点的那一条"并断言第 1 页同位置条目**未被改动**。
- **结果**：修复前该文件 **6/6 失败**（精确复现），修复后 **6/6 通过**。

写复现测试时的三条经验（都来自本项目）：

1. **测试必须先能失败**。如果一个"复现测试"在修复前就是绿的，它复现的不是这个缺陷。
2. **断言"未被影响的部分"**。只断言"我点的那条变了"无法区分"改对了"与"全改了"；分页 bug 正是靠"第 1 页未被改动"才被锁住。
3. **隔离单一规则**。`tests/test_smoke_set.py:74-94` 刻意把总量上限关掉（`ratio=1.0, max_count=1000`）来单独验证"维度规则"，
   否则上限的截断会先一步排除边界用例，测试就变成在验证上限。

### 3.2 变异测试是**必备**验证手段，不是可选项

做法：故意破坏实现，确认测试**能捕获**；捕获不了就是测试盲区，必须补测试。

本项目实际用过的真实变异（**全部已被捕获**；来源：提交 `6799acf`、`78f5aeb`、`fd87899` 各自的「验证」段落）：

| # | 变异 | 捕获它的测试方向 |
| --- | --- | --- |
| 1 | **退回旧动作词表**（12 个动作） | `test_actions.py`：`expectDisabled`/`expectState`/`upload`/`expectCount`/`expectNotVisible` 存在且被注入提示词 |
| 2 | **去掉断言门禁** | 断言存在性用例（实测 v1/v2/v3 分别有 5/4/1 条用例只有 `goto/fill/click`） |
| 3 | **`waitFor` 误作断言** | 专门用例锁定"`waitFor` 是同步动作，不算断言" |
| 4 | **提示词不列动作** | `render_actions_for_prompt()` 与提示词文本同源断言 |
| 5 | **去掉冒烟规模上限** | `test_smoke_set.py`：`SMOKE_MAX=25` / `SMOKE_RATIO=0.20` |
| 6 | **空产物放行**（退回 `.exists()`） | `test_stage_resume.py`：0 字节 `测试方案.md` 必须被拦 |
| 7 | **空需求放行**（`requirements: []`） | `test_stage_resume.py`：`incomplete` 判定 |
| 8 | **陈旧产物放行** | `test_stage_resume.py`：改了 PRD 后 `--stage 3` 必须报 `stale` |
| 9 | **哈希采样时机错**（在 `init_output` 之后才读清单哈希） | `test_stage_resume.py`：陈旧检测必须真的生效 |
| 10 | **`--from-stage` 退化为单阶段** | `test_stage_resume.py`：`--from-stage 2` 必须跑 2 和 3 |
| 11 | **preflight 误报**（不跳过本期上游产物） | `test_stage_resume.py`：`--stage all` 不得因阶段 1 未生成而报错 |
| 12 | **不使用重复键序列化**（`request_dict` 退回 `str(req.prd)`） | `test_service_api.py`：阶段 2/3 重建请求必须能还原 `Path` 列表 |
| 13 | **`generate` 不产出 UI 契约**（自动识别未接线） | `test_prototype.py:538-563`：断言 `ui-spec.json` 落盘 |
| 14 | **`_json` 不重试**（读方去掉 `PermissionError` 重试） | `test_service_api.py`：约 1/3 概率复现的读方竞态 |
| 15 | **导出忽略筛选** / **空结果静默放行** / **不重算追踪矩阵** / **不标注范围** / **文件名不带片段** | `test_export.py`、`test_casefilter.py` |
| 16 | **筛选维度内改交集** / **跨维度不再交集** / **标签改 AND** / **分面不忽略自身** | `test_casefilter.py` |
| 17 | **分面端点忽略筛选** / **导出端点忽略筛选** | `test_service_api.py` |
| 18 | 分页 bug 的 6 项：详情退回页内序号 ×2、标题编辑、状态切换、抽屉按下标取值、`keyOf` 退化为纯下标 | `StageEditorPaging.test.tsx` |

变异测试的**副产品**往往比变异本身更有价值：它会把"写的测试其实测不到东西"暴露出来。
本项目因此修正的假通过（原文记录在提交里）：

- **分面 `ignore="*"` 分支无覆盖** —— 逐维度 `ignore` 会先命中，`ignore_self` 走不到 → 补通配模式测试（`tests/test_casefilter.py:203`）。
- **追踪矩阵断言无法区分"重算"与"沿用"** —— 只断言"需求行存在"，全量矩阵里也有 → 改用**覆盖状态**判定（谎称已覆盖 → 重算后应为未覆盖）。
- **stub 只产出一个模块** —— 筛选与不筛选无从区分 → 补多模块 fixture。
- **深层文件与浅层文件同级** —— 让深层文件独占名称线索，才能锁住 depth 优先级（`tests/test_ingest.py:408`）。
- **冒烟用例给太少** —— 给 32 条用例时上限只有 6，边界用例根本轮不到（`tests/test_smoke_set.py:81`）。

### 3.3 「接线测试」的必要性：纯函数全绿 ≠ 它被调用过

这是本项目**反复踩到**的一类漏报：只测纯函数、没测调用点。

最典型的例子是 `priority.regrade`：纯函数测试一应俱全（`tests/test_priority.py:165-224`：赋值、记录原值、补 `@smoke`、不重复打标、缺 `tags` 字段、多需求取最高、无需求用默认、确定性），
但**把流水线里的那次调用删掉，全部测试仍然全绿**（`tests/test_pipeline_stub.py:59-61` 记录了这次漏报）。

因此本项目对每条机制都要求有**调用点断言**。已有的接线回归清单：

| 机制 | 接线断言位置 |
| --- | --- |
| 优先级重判被调用 | `tests/test_pipeline_stub.py:59-70`：落盘用例必须有 `prioritySource == "graded"` 与 `priorityBasis.dimension` |
| 语义去重被调用 | `tests/test_requirement_dedup.py:195-202`：从**报告文本**端到端验证 |
| 批次级允许集传入修复提示词 | `tests/test_cross_batch_dedup.py:289-292`："调用点是否传入批次级集合"是独立的一行 |
| 原型自动识别被接进阶段 3 | `tests/test_prototype.py:538-563`：`ui-spec.json` 必须落盘 |
| 凭证提示区分过期/未配置 | `tests/test_service_api.py:843-873`：**纯函数**直测 —— 因为 stub 模式下 `run_task` 走不到该分支，端点测试无法触达 |
| 旧版 `POST /runs` 不传 sessionId 的路径 | `tests/test_service_api.py:898` |

写接线测试的原则：**断言"可观测的产物痕迹"**，而不是断言"某函数被调用"。
例如判 `regrade` 是否接线，不看 mock 调用次数，而看落盘 JSON 里有没有 `prioritySource`；
判原型是否接线，看 `ui-spec.json` 是否存在。

### 3.4 变异的有效性：写变异前先确认它**真的改变了行为**

这条已被踩到三次，并已固化进实践（提交 `78f5aeb` 的原文说明）：

| 无效变异 | 为什么无效 | 正确写法 |
| --- | --- | --- |
| 只把 `return` 替换成别的 | `api.get` 仍在执行，副作用没变 | 删掉整个调用，或改调用参数 |
| `None or X` | 与原式**等价**（`None or X` 恒等于 `X`） | 真正删除标注，或改成 `X or None` 这类语义不同的写法 |
| 改一个未被任何路径读取的常量 | 行为没变 | 改成参与判定的那个值再跑 |

结论：**变异必须先跑一次"确认它让测试变红"**。如果变异没让任何测试变红，有两种可能，
必须逐个排除：① 变异无效（行为没变）；② 真的有盲区（要补测试）。

### 3.5 测试信号来源要正确：超时是最容易被误读的信号

真实教训：曾把 `--testTimeout` 设得过紧，一个正常耗时约 **6s** 的用例被**假超时**，
于是变异看起来"被捕获"了 —— 实际是被**错误的测试**（超时）捕获，与变异本身无关。
去掉覆盖后用默认超时重跑，才拿到干净信号。由此固化的做法：

- 不要用"更紧的超时"换取更快的反馈；超时与被测行为无关，只会污染信号。
- `ui/vitest.config.ts` 因此把 `testTimeout` / `hookTimeout` **放宽到 20s**：antd 组件在 jsdom 下
  首次渲染常需 1~3s（含 rc-* 动画与 ResizeObserver 兜底），默认 5s 会让含多次 `findBy*` 的用例随机超时
  （实测出现过 flaky：单独跑通过、并发时失败）。
- 后端同理：`test_llm_stream.py`、`test_service_api.py` 全走**离线录制片段**，没有真实网络等待。

### 3.6 测试基础设施的经验

- **fixture 要自包含**：合成 docx/zip/pdf、录制 SSE 片段，不依赖外部文件或网络（`tests/test_ingest.py`、`tests/test_llm_stream.py`）。
- **别把断言门禁留成历史债务**：提交 `6799acf` 加了"每条用例至少一个断言"后与 stub 生成器冲突（stub 只产出 `goto`），
  修 stub 后 42 处失败降到 3 处，其余为内联 fixture，逐一补齐断言步骤。
- **兼容性护栏要显式**：`test_llm_stream.py` 断言"已产出内容后失败绝不重试"、`test_realtime.py` 断言"不订阅 metrics 的客户端
  不应收到 `run_metrics`" —— 这类"不得做某事"的断言防止重构把已优化的行为改回去。

## 4. 平台注意事项（Windows）

### 4.1 `__pycache__` 必须清理

改动 `src/prd2tc/**` 后若行为与代码不符，先清 `__pycache__`（`.gitignore:2-3` 忽略它，但本地残留的旧字节码可能被加载）。
仓库里已存在大量 `.pyc`，它们在跨版本解释器切换、`pip install -e .` 前后混用、时间戳精度等情形下都可能不失效：

```powershell
Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
```

### 4.2 变异脚本必须终止**整棵进程树**

这是本仓库最贵的一次教训：`subprocess.run(shell=True, timeout=…)` 超时只会杀掉 **shell**，
`node` / `vitest` 等**孙进程会存活**并继续持有管道，于是 `communicate()` **永久挂起** ——
实测导致脚本挂死，并把源码留在**变异态**。正确做法是用 `taskkill /F /T` 终止整棵树。

```python
# 变异 → 跑测试 → 无论如何都还原（示意）
target = pathlib.Path("src/prd2tc/priority.py")
original = target.read_text(encoding="utf-8")

def run(cmd, timeout=300):
    proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], check=False)  # /T 连孙进程
        return proc.communicate()

try:
    target.write_text(original.replace("def regrade(", "def regrade_DISABLED("), encoding="utf-8")
    run(["python", "-m", "pytest", "tests", "-q"])
finally:
    target.write_text(original, encoding="utf-8")   # 无条件还原，放在最外层
```

两条铁律：① `finally` 里**无条件还原**源码，且写在**最外层**（抛异常、Ctrl-C、子进程挂死都不能跳过）；
② 还原后**校验一次**（比对哈希），不要凭"我写了 finally"就放心。

### 4.3 杀进程前**必须先确认目标**

曾差点杀掉工作台自身的 `node` 进程。正确做法：**先列 PID 与命令行核对**，再杀。

```powershell
# 先看：确认哪个 PID 是"我这次变异启动的 vitest"，而不是工作台的 vite/node
Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
  Select-Object ProcessId, ParentProcessId, CommandLine | Format-List
taskkill /F /T /PID <核对过的 PID>      # 再杀：只杀核对过的那个，且用 /T 连子进程
```

反面模式：按进程名批量杀（`taskkill /F /IM node.exe`）——会连你的开发服务器、编辑器插件一起杀。

### 4.4 Windows 的 `os.replace` 会让**读方**也拿到 `PermissionError`

这不是"写方没写好"，而是 Windows 的共享冲突语义：目标文件正被打开或正被替换时，
**读方**同样会拿到 `PermissionError`。**写方的重试救不了读方**。因此项目里有两处对称的重试：

| 位置 | 策略 | 为什么 |
| --- | --- | --- |
| `io.atomic_write_text`（`io.py:30-54`） | 写临时文件 + `fsync` + `os.replace`，`PermissionError` 重试 5 次（退避 20/40/60/80ms），仍失败才抛 | 实时心跳与状态轮询会并发读写同一文件 |
| `WorkspaceStore._json`（`service.py:168-196`） | 读 `PermissionError` 重试 6 次（退避 20/40/…/120ms）；**重试仍失败返回默认值而不抛错** | 产物文件（`run-manifest.json` 等）由 pipeline 直接写入、**不经过 store 的锁**，锁也挡不住该竞态。实测 `GET /runs/{id}` 在运行中约 1/3 概率 500，帧就落在 `_json` 的 `read_text`；修复后连跑 8 次全过 |

注意两者的**失败策略不同**，这是有意为之：写失败必须让调用方知道（否则静默丢产物）；
读失败则不应让整个请求 500 —— 读不到某个辅助产物只意味着前端少一块信息。

### 4.5 其它平台相关注意

- **编码**：`cli.py:18-21` 对 `sys.stdout`/`sys.stderr` 显式 `reconfigure(encoding="utf-8", errors="replace")`。
  GBK 控制台里 `Get-Content` 读 UTF-8 源码看到的乱码是**终端解码**问题，不是文件坏了（用 `-Encoding utf8`）。
- **路径分隔符**：产物路径与指纹一律用 `Path.as_posix()` 或 `Path` 对象（`io.py:158`）。
- **产物目录是运行期产物**：`.prd2tc-workspace/` 与 `output/` 都被忽略（`.gitignore:36-46`），别把它们的行为当契约。

## 5. 提交约定

从 `git log --oneline`（本仓库共 21 次提交）归纳出的**实际**风格：

### 5.1 格式

```
<type>[(scope)]: <中文摘要>

## 现象与根因 / 需求 / 问题 …（为什么会有这个改动）
## 变更 / 修复 …（改了哪些文件、做了什么）
## 验证 …（测试数量、变异项、端到端断言）
## 影响 / 附带 …（顺带修了什么、有什么副作用）
```

- **摘要用中文**，`type` 用英文前缀。实际用过的前缀：`fix:`、`feat:`、`docs:`、`test:`、`fix(ui):`、`feat(ui):`、`test(ui):`、`release:`。
- **scope 用目录/子项目名**：`ui`、`test`、`docs`。
- 摘要要写**用户可见的结果**，不是"改了什么文件"：
  - ✅ `fix(ui): 分页后「详情」与编辑定位错行（数据损坏级 bug）`
  - ✅ `fix: 阶段推进误报「未配置 LLM 凭证」（旧会话未被新会话替换）`
  - ✅ `feat: 补全动作词表（12→23）+ 断言门禁 + 阶段产物完备性与分段执行`
  - ❌ `更新 StageEditor.tsx`

### 5.2 正文必须写「根因 → 修复 → 验证」

本仓库的提交正文普遍长且结构化，例如 `fd87899`：

- **现象与根因**：贴出问题代码 `render:(v, row, i)=><Button onClick={()=>setSelectedCase(i)}>`，
  并解释"antd Table 的 `index` 是当前页内序号"。
- **影响比"显示错行"更严重**：说明同一错误也用在 `change(i, …)` 上，因此是**静默数据损坏**。
- **修复**：引入 `keyOf(row, index)` 作为稳定标识，列出所有改动的调用点。
- **验证**：`修复前该文件 6/6 失败（精确复现），修复后 6/6 通过` + `6 项变异全部被捕获` + 全仓库核查结论。

### 5.3 把**实测数据与教训**写进 commit message

这是本仓库最有辨识度的习惯，也是最有价值的部分。真实例子：

| 提交 | 写进正文的数据/教训 |
| --- | --- |
| `6799acf` | `check` 误用率 **87%~91%** 的逐条枚举表（23/9/5/4/2 次）；`TOOL 仅 3 条需求就撞满 8192 → ≥2731 tok/需求`；测试 `580 全绿（+26）`；**10 项变异全部被捕获** |
| `78f5aeb` | 实测串行/并发耗时、`GET /runs/{id}` 约 **1/3 概率** 500 且修复后连跑 8 次全过；`后端 663 全绿（+54）`、`前端 66 全绿（+24）`；**12 项后端变异全部被捕获**；并主动记录"我自己的变异无效（`None or X` 与原式等价）" |
| `fd87899` | `6/6 失败 → 6/6 通过`；"已核查全仓库：无其它使用 Table `render` 的 `index` 做选择或改数据的位置" |

**为什么要这么做**：这些数字是后续判断"这个机制是否真的有效"的唯一依据。
没有 `87%~91%` 这个数，就无法论证"补齐词表比反复强调不要滥用 `check` 更有效"；
没有 `1/3 概率复现`，就无法说服别人那个重试不是过度设计。

### 5.4 文档/约定变更与代码变更**分开提交**

实际历史里 `docs:` 与 `feat:`/`fix:` 是分开的提交（如 `9ebf671 docs: 评测报告 v3`、
`3998095 docs: 新增 6 份设计与评测文档`、`1f4a28b docs: 测试用例质量评测报告 v2`）。
这样做的好处：代码提交的 diff 保持"可审查的最小行为变更"，
文档提交可以独立review、独立回滚，也不会让 `git blame` 的行为归因被文档噪声污染。

### 5.5 提交前自检

```bash
python -m pytest tests -q                       # 必须全绿
python scripts/sync_prompts.py --check          # 若动过 prompts/
cd ui && npm test && npm run build              # 若动过 ui/
git status                                      # 确认没有把 .prd2tc-workspace/ / output/ / __pycache__ 带进来
```

## 6. 文档维护约定

### 6.1 `docs/` 只放**长期有效**的文档

- 过程性记录（评测草稿、修复计划、实施日志、交接备忘）**不进仓库**。
  `.gitignore:48-54` 用忽略清单落实这一点：

  ```
  （历史过程文档与临时排障记录不作为公开文档链接）
  ```

- 当前公开、长期有效的开发文档包括 `docs/pipeline.md`、`docs/case-spec.md`、
  `docs/export.md`、`docs/ingestion.md`、`docs/prompts-and-gates.md`、本文与 `docs/operations.md`。

> 被忽略或不存在的历史文档不得继续出现在公开文档索引；门禁说明以 `docs/prompts-and-gates.md` 和 `docs/operations.md` 为准。

### 6.2 提示词改动后必须同步 assets 副本

`loader.load_asset` 的查找顺序是「仓库内 `prompts/` 优先，安装包 `assets/prompts/` 兜底」
（`loader.py:22-36`）。这意味着**两处不一致时，本地开发看到的是新提示词、而安装用户拿到的是旧副本**。

```bash
python scripts/sync_prompts.py           # 同步 prompts/*.md → src/prd2tc/assets/prompts/
python scripts/sync_prompts.py --check   # 只检查（CI 用），不一致则退出码 1
```

`sync_prompts.py` 做**双向**检查：正向比对内容，反向报出「assets 里存在但 prompts/ 已删除」的文件
（`scripts/sync_prompts.py:41-44`）。

为什么需要这个脚本（提交 `6799acf` 原文）：`assets/prompts/` 是被 `package-data` 打包的安装包兜底副本，
但此前**无同步机制、无一致性测试**，实测已有 **3 个文件**与 `prompts/` 不一致。脚本做**双向**检查
（`scripts/sync_prompts.py:41-44`）。改提示词的完整清单：

1. 改 `prompts/*.md` 并**升版本号**（资产头部的 `> prompt vX.Y.Z`，`cli.py:219-223` 会读它）。
2. `python scripts/sync_prompts.py`。
3. 跑 `tests/test_prompt_engineering.py`、`tests/test_gate_constraints.py` 与一致性测试。
4. ⚠ 改了提示词会让 `promptHash` 变化 → 所有历史 `--resume` 检查点失效（`pipeline.py:288-295`）。这是**预期行为**，别试图绕过。

### 6.3 文档与代码的一致性

文档里出现的枚举（动作词表、优先级、模块码、配置默认值）**必须与代码同源**：

- 动作词表：唯一权威是 `actions.ACTION_SPECS`，提示词文本由 `render_actions_for_prompt()` **生成**。**新文档不要手抄枚举**，用"见 `actions.py`"或让代码生成。
- 配置默认值：以 `config.py` / `stages.py` 为准；会话级配置还需检查服务层如何覆盖分阶段配置。

## 7. 新增能力的检查清单

### 7.1 加一个**门禁**

1. 实现校验函数 → `src/prd2tc/gates.py`（返回错误列表，空 = 通过；错误文案要能让模型看懂）。
2. 接进阶段流程 → `src/prd2tc/stages.py` 的 `generate_with_feedback(...)` / `run_stage_3_modules(...)` 调用点。
3. 提示词也说明该要求 → `prompts/00-testing-principles.md`（自检门）或对应阶段 prompt + `assets/prompts/` 副本。
4. 字段级约束 → 同步 `stages.field_spec()`（字段约束与 validator 枚举必须同源，`stages.py:126-154`）。
5. 单测 → 新文件或加到 `tests/test_gates.py` / `tests/test_gate_constraints.py`。
6. **接线测试** → 断言"门禁真的在流水线里生效"（参照 `tests/test_pipeline_stub.py` 的落盘断言）。
7. **变异** → 至少 2 项：① 去掉门禁；② 条件放宽到永远通过。都必须被捕获。
8. 检查 fixture → stub 生成器（`src/prd2tc/stub.py`）要能满足新门禁，否则会像 `6799acf` 那样一次红 42 处。
9. 文档 → `README.md` 的门禁表 + `docs/pipeline.md`。

> **特别注意**：新门禁不得拦掉 prompt 自带的示例 —— 否则模型陷入「照抄示例 → 被拦 → 改写」死循环
> （`tests/test_gate_constraints.py` 文件头记录的 R-1 根因）。

### 7.2 加一个**动作**

1. 加 `ActionSpec` → `src/prd2tc/actions.py` 的 `_OPERATIONS` 或 `_ASSERTIONS`，填 `kind`/`summary`/`needs_data`。
2. 版本号 → `src/prd2tc/schema.py` 的 `ACTION_SCHEMA_VERSION`（新版本应是旧版**超集**，旧产物才能继续校验）。
3. 提示词 → **不需要手改**（`render_actions_for_prompt()` 自动生成），但要跑 `sync_prompts.py --check` 确认副本一致。
4. 断言类额外确认 → 「每条用例至少一个断言」门禁会把它算进去（`actions.is_assertion`）。
5. 单测 → `tests/test_actions.py`：存在性、分组、是否出现在注入文本里、`needs_data` 语义。
6. 文档 → `docs/case-spec.md`（若其中列了动作，改为引用而非手抄）。
7. **变异** → 退回旧词表，必须被捕获。

### 7.3 加一个**导出格式**

1. 实现构造器 → `src/prd2tc/export/<fmt>.py`，导出 `build_<fmt>_bytes(cases, …) -> bytes`；**用标准库**
   （ZIP + XML/JSON 子集），不引第三方。
2. 接线 → `src/prd2tc/export/__init__.py`：加进 `MEDIA_TYPES`、`__all__`、`export_cases` 的 `formats` 分支（含 `--out` 后缀判定）。
3. CLI → `src/prd2tc/cli.py` 的 `export` 子命令加进 `--format` choices。
4. 服务下载 → `src/prd2tc/service.py`：`GET /api/v1/exports/{run_id}/{fmt}` 与 `_download_name` 的扩展名映射（`service.py:68-83`）。
5. 确定性 → ZIP 时间戳与节点 id **必须确定性生成**，否则 `compare-runs` 会误报漂移（`docs/export.md`）。
6. 失败不阻断 → pipeline 里的导出必须包在**局部** `try/except` 里，降级为告警（`pipeline.py:504-514`）。
7. 单测 → `tests/test_export.py`：按"回归 R-*"风格为每个已知缺陷留一条用例；并验证可被严格解析器读取。
8. 筛选兼容 → 若支持筛选导出，必须**按子集重算**追踪矩阵并在统计页标注"这是子集"（`export/__init__.py:104-127`）。
9. 文档 → `docs/export.md` 与 `README.md` 的导出表。

### 7.4 加一个**配置项**

1. `Config` 字段 + 默认值 → `src/prd2tc/config.py`（dataclass 字段 + `from_env` 解析；布尔值沿用
   `in {"1","true","yes","on"}` 的既有风格）。
2. 是否影响产物 → 决定它进不进 `configHash` / `inputHash`（`pipeline.py:296-313`）。
   影响产物却**不进哈希**会导致旧检查点被错误复用。
3. 写入 `run-manifest.json` 的 `parameters`（`pipeline.py:328-334`）—— 排障时唯一能回答"当时用的什么参数"的地方。
4. 模块级常数 → 若读在 `stages.py` 模块级（如 `CTX_TOP_K`），注意**导入时读取一次**：改环境变量需重启进程，
   测试要用 `monkeypatch.setenv` + `importlib.reload`，或改成函数内读取。
5. CLI 开关 → 若要给等价开关，加在 `cli.py:294` 那一行的 `generate` 参数里，并在 `cmd_generate` 用 `replace(cfg, …)` 覆盖。
6. 单测 → 环境变量生效、非法值回落、默认值正确。参照 `tests/test_concurrency_defaults.py` 的四条断言（默认 / 生效 / 非法回落 / 显式覆盖）。
7. 文档 → `README.md` 配置表 + `docs/operations.md` §1 与附录（含"调它会发生什么"）。

### 7.5 通用收尾

1. `python -m pytest tests -q` 全绿，数量以当前执行输出为准。
2. `cd ui && npm test` 全绿、`npm run build` 通过（动过前端时）。
3. `python scripts/sync_prompts.py --check`（动过 `prompts/` 时）。
4. 变异测试：至少 2 项**已确认真实改变行为**的变异被捕获（§3.4）。
5. 接线测试：新机制必须有"调用点/产物痕迹"断言，而不只有纯函数测试（§3.3）。
6. 提交信息按 §5 的格式，把实测数据与教训写进正文；文档变更单独提交（§5.4）。
