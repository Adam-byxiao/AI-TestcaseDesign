# prd2tc 应用演进对比：初版 → 当前（问题与优化全记录）

> 定位：开发后备文件。供新接入开发者快速理解「应用从哪来、踩过哪些坑、为什么长成这样」，
> 以及每条优化对应的根因、做法、验证与当前状态。
>
> - 基线（初版）：git commit `5c73466`（`v0.1.0`，仓库唯一已提交版本）
> - 当前：工作区（`__version__ = "0.3.0"`，40 项改动/新增尚未提交）
> - 快照时间：见文件修改日期。数据来源：会话实测、git 差异、`.prd2tc-workspace/outputs/*/run-manifest.json`。

---

## 1. 快照对比：初版 vs 当前

### 1.1 仓库形态

| 维度 | 初版 v0.1.0（5c73466） | 当前（工作区） |
| --- | --- | --- |
| 代码体量 | 41 个文件；核心 `cli/stages/gates/validator/llm/config/io` | 新增 11 个 src 模块 + React UI + 9 个测试/文档文件 |
| 编排层 | CLI 直接调用（`scripts/orchestrate.mjs` 存在于仓库） | `PipelineRunner`（pipeline.py）+ FastAPI `service.py` + React 工作台（ui/） |
| Provider | 仅 DeepSeek/OpenAI 兼容（config 固定 deepseek） | openai / anthropic / ollama 三选一；MiniMax anthropic 兼容实测（M3） |
| 阶段能力 | 单次生成（一次输出六章/全部用例） | 长文档分段并发 + 确定性渲染 + 按模块分批生成 + 检查点/恢复 |
| 用例形态 | 全量一次出；module 静态表 | 模块化 + 机器重编号 + module 锚定归一 + 动态 allowed 校验 |
| Token 度量 | 无 | 阶段/模块级账单、缓存命中字段、可复现指纹 |
| 测试 | 6 个测试文件 | 10 个测试文件，94 passed |

### 1.2 代码级差异统计（git diff --stat 5c73466）

- 已跟踪文件改动：**19 个文件，+1426 / -511**
- 高增量文件：`stages.py +714`、`cli.py ±508`（重写）、`llm.py 171`、`io.py +123`
- 新增文件（未跟踪）：`pipeline/service/stub/chunking/diff/traceability/schema/models/execution/profiles/reproduce`、
  `ui/`（React 工作台）、4 个新文档、5 个新测试
- 改动文件：`prompts/01-prd-analysis.md`（v0.4.0→v0.5.0）、`config/validator/gates/llm/io`、README、pyproject

---

## 2. 问题与优化分类详录（按发生顺序）

> 每条含：现象 / 根因 / 方案 / 验证。

### 2.1 长文档与输出截断（阶段1、阶段3）

| # | 问题 | 根因 | 方案 | 验证 |
| --- | --- | --- | --- | --- |
| 1 | 长 PRD（28K 字）单次分析被截断，缺少「业务规则/非功能需求/风险与疑点」章节 | 单次输出超 max_tokens；OpenAI 客户端**未发送 max_tokens**、未记录 finish_reason | 发送 `max_tokens`；捕获 `finish_reason`；截断时给出「精简输出」诊断提示 | 长 PRD 复跑六章齐全 |
| 2 | 阶段1 六章合并仍可能截断 | 第 1/3 章体积最大，硬要 LLM 输出 | 第 1/3 章**程序化渲染**（`render_analysis_head`/`modules_from_analysis`），LLM 只合并四个小章节（`FOUR_SECTION_PROMPT`） | stub/真实长文档均六章齐全 |
| 3 | 阶段3 大模块输出截断（PROD 曾截断、CART 尾部丢失） | 整模块一次输出超限 | 按模块分批（`CASE_BATCH_SIZE`，后升级为 P3 自适应） | 模块级失败率下降 |

### 2.2 用例生成的模块一致性与「module 非法 / 不匹配」

| # | 问题 | 根因 | 方案 | 验证 |
| --- | --- | --- | --- | --- |
| 4 | 门禁报 `module 非法: CHECKOUT/COMMON` | 阶段1 prompt 引导模型用约定代号（CHECKOUT/COMMON），而 PRD 真实前缀是 `CHK/GLB`；`validator.MODULES` 静态表过时（缺 CHK/GLB/DET/LIST/ERR，却含别名 COMMON/CHECKOUT）；`field_spec()` 把静态表注入阶段3 | **三层修复**：①prompt v0.5.0 强制「模块代号优先复用 PRD 编号前缀、禁同义模块」；②`requirements_from_analysis` 按「原 GLB-001」引用与节标题别名（`CHECKOUT→CHK/ORDER→ORD/DETAIL→DET`）机器级归一；③`field_spec` 改通用提示（模块码由批次注入）、`renumber` 确定性强制 `module=批次模块` | 86+ 测试；真实 PRD 上 COMMON/CHECKOUT/ORDER 重复模块被消除 |
| 5 | 同一条 PRD 需求映射到两个 REQ（COMMON↔GLB、CHECKOUT↔CHK） | 模型自拟模块名与 PRD 前缀不一致 → 需求重复 | 同上三层 + 需求按指纹去重 | 归一后模块分布无重复 |
| 6 | 用例 `module`/`id` 前缀/`requirement` 前缀三者不一致 | 模型自由填 module、随手写 id | 按模块批处理中 `renumber`（TC-<MOD>-001…）+ `module` 强制 + `wrong_refs` 校验（只能引用本模块真实 REQ） | 端到端断言三者一致 |

### 2.3 Token 成本（重点，量化驱动）

**观测样本（真实 MiniMax-M3 运行）**

| 阶段 | tokens | 耗时 | 说明 |
| --- | --- | --- | --- |
| 分析 | 35,395~96,445 | 186~470s | 分段×重试放大 |
| 方案 | ~17K | ~30~57s | — |
| 用例（ERR 模块 3 需求→7 用例） | 10,074（prompt 7958 + comp 2116） | — | **prompt 占 79%** |

**case 生成 prompt 构成实测拆解（一次性样本）**：module_context（整份 PRD ≈30K 字）≈71%、测试方案 ≈21%、约束/任务 ≈8%——即 **71% 的输入是每批重复发送的整份 PRD**。

| # | 方案 | 根因/动机 | 内容 | 状态/收益 |
| --- | --- | --- | --- | --- |
| 7 | P0-a 方案移入稳定 system 前缀 | 方案/约束/示例被逐模块重复发送 | `stage_3_system()`；user 只留可变内容 | ✅ |
| 8 | P0-b 门禁重试增量修正 | 失败全量重发大输入 ×2~3 | `repair_builder`/`stage3_repair_builder`：只回传失败输出+错误+真实 REQ | ✅ |
| 9 | P1-a 结构化事实源 | 阶段2 输入是冗长 Markdown | `compact_analysis`：需求大表→一行一需求 | ✅ |
| 10 | P1-b 四小章合并瘦身 | 合并输入含重复的 1/3 章 | `_compact_partial` 剥离 | ✅ |
| 11 | P2 度量先行 | 无法知道钱花在哪 | `moduleTokens`（run-manifest 阶段/模块 token 账单） | ✅ |
| 12 | **修正：token 累计含重试** | manifest `tokens` 只记最后一次，**低估真实消耗** | `generate_with_feedback` 累积所有轮次 prompt/completion | ✅（加测试） |
| 13 | **P0-d module_context 瘦身** | 整份 PRD 每批重发（占 prompt 71%） | chunk_size 用配置 6000 + `CTX_TOP_K=2` 精准取段 | ✅ 上下文 153K→43K token（-72%），调用 27→19 |
| 14 | **P3 批次容量自适应** | 固定 8 需求/批 × 固定开销被放大 | `_module_batch_size`（输出预算 ÷450 估 token，上限 24），小/中模块单模块一次全量 | ✅ 估算 case 阶段 ~280K→~150K |
| 15 | P0-c Prompt 缓存探测 | MiniMax M3 兼容端点「被动缓存」，官方主动缓存模型表未含 M3 | `cache_control`（env `PRD2TC_PROMPT_CACHE`，不报错不保证生效）+ `usage.cache_read/creation_input_tokens` 捕获 + `prd2tc probe-cache` | 🔍 待实跑确认回显 |
| 16 | 缓存字段贯通 | 真实运行要看命中量 | LLMResult/gate 重试/模块级 moduleTokens/CLI 账单均带 cache 字段 | ✅ |

### 2.4 可复现性（结构级）

| # | 问题/目标 | 方案 | 状态 |
| --- | --- | --- | --- |
| 17 | 每轮结果不完全一致 | 明确定义：**字节级不可承诺**（厂商亦不承诺）；目标是**结构级复现** | 已实现 |
| 18 | L1 可复现模式 | `Config.repro/seed`；repro 下三阶段温度归零；manifest/configHash 记录 repro+seed；OpenAI 兼容请求带 `seed` | ✅ |
| 19 | L2 结构指纹 + 轮间漂移报告 | `reproduce.py`：`run_fingerprint`（hash+模块/REQ/用例数/覆盖率/门禁，排除文本）；成功运行落盘 `reproduce.json`；`prd2tc compare-runs`（`--fail-on-drift` 可入 CI） | ✅ 94 测试 |
| 20 | 说明 | Web 对拍入口、L3 Flaky 降级重试未做（列为可选项） | — |

### 2.5 工程与质量闭环（v0.1.0 → 现在新增能力）

| 能力 | 说明 |
| --- | --- |
| Web 服务 + 工作台（service.py + ui/） | PRD 上传/版本、运行列表、逐阶段审阅（草稿+批准/驳回）、WebSocket 事件、Token 消耗卡片、LLM 会话配置 |
| Web 一键运行 | `auto` 模式 `stage="all"` 一次跑完并跳过人工审阅，审阅记录标记 `one-click`；失败定位到子阶段 |
| 需求 Diff / 增量合并 / 执行结果导入 / 示例晋级 | `diff.py`、`merge`、`import-results`、`promote-examples`；`traceability.py` 双向追踪 |
| 模块 profile / 示例库（few-shot/RAG） | `profiles.py`、`examples.py`、prompt 04 学习约束 |
| 失败留档 | 每模块门禁失败保存 `.failures/<模块>.txt` 原始输出，便于复盘 |

### 2.6 修正记录与编码教训（易被忽略但重要）

| 教训 | 说明 |
| --- | --- |
| PowerShell 5.1 写文件编码损坏 | Get-Content/Set-Content 字符串替换会破坏 UTF-8 中文（mojibake）；一律用 write/edit 工具 |
| CLI 解析器遮蔽 | 子命令名 `prompts` 曾遮蔽主 parser；改名规避 |
| 资源根路径 | `loader._REPO_ROOT` 用 `parents[N]` 定 01 根目录，改层级曾读到错误 prompts（被包内资源兜底掩盖） |
| 旧参数兼容 | 批次覆盖保留 `PRD2TC_CASE_BATCH_SIZE` 旧名；新增参数均带默认值并记录于 docs |
| prompts 双份同步 | `prompts/`（开发态优先）与 `src/prd2tc/assets/prompts/` 需保持字节一致 |
| 测试与行为耦合 | 改批次/重编号后同步更新依赖旧默认的测试（10→40 需求解耦） |

---

## 3. 验证与指标

- 测试：`pytest tests -q` → **94 passed**（test_reproduce/test_service_api/test_long_doc/test_new_core 等新增）
- 前端类型：`npx tsc -b` 通过（沙箱内 `vite build` 受 EPERM 限制，需在正常终端构建）
- 真实运行样例 token 账目见 §2.3 表；完整可复跑：`.prd2tc-workspace/outputs/*/run-manifest.json`（含 moduleTokens/cache 字段）

---

## 4. 未决 / 边界 / 风险

1. **P0-c 缓存**：MiniMax M3 兼容端点 `cache_control` 是否命中，须实跑 `prd2tc probe-cache` 看 `cache_read_input_tokens` 回显；未命中则 21% 的 system 前缀成本仍全价。
2. **字节级复现不承诺**；结构级漂移需用 `compare-runs` 度量。
3. **L3 Flaky**（模块偶发失败自动降级分批重试）未实现；当前靠批次自适应 + 门禁重试兜底。
4. **Web 对拍入口**（运行指纹对比）未做。
5. **仓库状态**：当前全部优化仍为未提交改动（HEAD=5c73466）；建议按模块分批 commit/push。
6. module 锚定对「跨多个前缀的混杂模块」（历史遗留 PROD/SEARCH）不自动拆分，靠 prompt 源头规避。

---

## 5. 维护建议

- **提交节奏**：建议分 6~8 个提交（基建/token/一致性/复现/Web/文档），避免单次巨型 diff。
- **回归工作流**：验收一律 `--repro`（或服务端 `PRD2TC_REPRO=1`）；两轮 `compare-runs --fail-on-drift`。
- **docs 即契约**：token-optimization / reproducibility / gate-troubleshooting 随实现同步更新。
- **双份 prompts 同步**：改 prompt 时必须同时改 `prompts/` 与 `assets/prompts/`，并升版本号。

---

## 附录：命令速查（当前）

```text
prd2tc generate --prd <PRD> [--repro] [--seed N] [--stub|--provider ...] [--jobs N] [--profile ...]
prd2tc compare-runs <RUN_A_DIR> <RUN_B_DIR> [--out x.json] [--fail-on-drift]
prd2tc probe-cache [--provider anthropic] [--api-base ...] [--model ...]
prd2tc diff/merge/import-results/promote-examples/validate/stats/prompts
```
