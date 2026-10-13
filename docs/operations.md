# prd2tc 运维手册：配置、运行与排障

> 本文只描述**已实现**的行为，每条非显然结论都标注了源码位置。
> 配置默认值来自 `src/prd2tc/config.py`；批次容量与温度的默认值来自 `src/prd2tc/stages.py`。

## 1. 配置项总表

### 1.1 Provider 与连接

| 变量 | 默认值 | 作用 | 调它会发生什么 |
| --- | --- | --- | --- |
| `PRD2TC_LLM_PROVIDER` | `openai` | 选择 Provider，取值 `openai` / `anthropic` / `ollama` | 非法值直接 `ValueError` 拒绝启动（`config.py:52-53`）。它也决定默认地址、默认模型与是否需要 API Key（`requires_api_key`，`config.py:46-47`） |
| `PRD2TC_API_KEY` | 空 | Provider 凭证 | openai/anthropic 必填；ollama 忽略。openai 分支的回退链是 `PRD2TC_API_KEY` → `OPENAI_API_KEY` → `DEEPSEEK_API_KEY`（`config.py:63`），anthropic 是 `PRD2TC_API_KEY` → `ANTHROPIC_API_KEY`（`config.py:56`） |
| `PRD2TC_API_BASE` | 随 Provider | API 地址（**不是**官网/控制台地址） | 尾部 `/` 会被去掉（`config.py:69`）。openai 分支还会回退到 `OPENAI_BASE_URL`（`config.py:64-66`）。填错时不会立刻报错，直到 `check-llm` 或首次真实请求才暴露（见 §3.2） |
| `PRD2TC_MODEL` | 随 Provider | 模型名 | 只影响请求体里的 `model`，不做本地校验；模型名不被端点支持时表现为 HTTP 400（`llm.py:604-606`） |
| `PRD2TC_TIMEOUT` | `120` | 单次 HTTP 请求超时（秒） | 调小会让长输出提前中断（表现为 `URLError`/超时，非截断）；探测请求另有 25s 上限（`llm.py:433`、`llm.py:558`） |
| `PRD2TC_MAX_RETRIES` | `3` | **HTTP 层**重试次数，指数退避 `2**attempt` 秒（`llm.py:65`） | 只对 `{429,500,502,503,504}` 生效（`llm.py:46`）；**已经产出内容后绝不重试**，避免重复拼接与重复计费（`llm.py:98-103`）。注意它与 CLI 的 `--max-gate-retries`（门禁反馈次数）是两个独立旋钮 |

Provider 默认组合（`config.py:54-63`）：

| Provider | 默认 `api_base` | 默认模型 |
| --- | --- | --- |
| `anthropic` | `https://api.anthropic.com` | `claude-sonnet-4-5` |
| `ollama` | `http://localhost:11434` | `llama3.2` |
| `openai`（无 `DEEPSEEK_API_KEY`） | `https://api.openai.com/v1` | `gpt-4.1-mini` |
| `openai`（有 `DEEPSEEK_API_KEY`） | `https://api.deepseek.com` | `deepseek-chat` |

### 1.2 输出预算与批次容量（最容易误用的一组）

| 变量 | 默认值 | 作用 | 调它会发生什么 |
| --- | --- | --- | --- |
| `PRD2TC_MAX_TOKENS` | `16384` | 单次请求的**输出上限** | 它只是上限、按实际输出计费，**调高不增加固定成本**（`config.py:10-13`）。但它会参与 configHash，改了它旧检查点会失效（`pipeline.py:310-312`）。不得超过 Provider 的模型输出上限，Anthropic 尤其严格 |
| `PRD2TC_TOKENS_PER_REQ` | `1800` | 批次容量估算：每条需求预期输出 token | 调小 → 每批需求数变多（更容易截断）；调大 → 每批变小、请求数变多。默认值来自实测校准：唯一完整跑完的 KB 模块是 1700 tok/需求，被截断的 TOOL 模块仅 3 条需求就撞满 8192（`stages.py:324-326`）。旧值 450 严重偏低，是运行 `9d97060e` 大面积截断的直接原因 |
| `PRD2TC_CASE_BATCH_CAP` | `8` | 单批需求数硬上限 | 防止批次随预算无限膨胀；旧值 24 可让 30+ 条需求挤进一批（`stages.py:332`） |
| `PRD2TC_CASE_OUTPUT_BUDGET` | `0.60` | 单批允许占用 `max_tokens` 的比例 | **调高它会让 `PRD2TC_MAX_TOKENS` 失效**：批次容量与 `max_tokens` 成正比，安全余量恒为 `1/该值`。沿用 0.85 只有 1.18x 余量；降到 0.60 才有 1.67x（`stages.py:328-330`） |
| `PRD2TC_CASE_BATCH_SIZE` | 未设置 | **硬覆盖**每批需求数（旧参数兼容） | 一旦设置，上面三个常数全部被绕过，批次大小被钉死为 `min(该值, 模块需求数)`（`stages.py:340-342`）。只在排障时用，不要长期设 |
| `PRD2TC_CTX_TOP_K` | `2` | 每需求携带的 PRD 原文段落数 | 调小更省 token 但上下文变薄；`0` = 完全不携带原文上下文（`stages.py:316-317`） |

批次容量的实际公式（`stages.py:338-345`）：

```
budget = max(2000, max_tokens × OUTPUT_BUDGET_RATIO)      # max_tokens 为 0 时取 7000
batch  = max(1, min(budget ÷ TOKENS_PER_REQ_EST, CASE_BATCH_CAP, 模块需求数))
```

按默认值代入：`budget = max(2000, 16384×0.60=9830) = 9830`，`9830 ÷ 1800 = 5`，
即**默认每批最多 5 条需求**（受 `CASE_BATCH_CAP=8` 与模块实际需求数进一步限制）。

**四个参数一起调会怎样**（把公式代进去就能预判）：

| 改动 | 批次容量 | 实际效果 |
| --- | --- | --- |
| `MAX_TOKENS` 16384 → 32768（其余不动） | `5 → 10`，但被 `CASE_BATCH_CAP=8` 截到 8 | 单批需求数变多、**输出余量比例不变**（仍是 1.67x），请求数减少、单次风险上升 |
| `MAX_TOKENS` 16384 → 8192 | `5 → 4` | 单批更小、更安全，但请求数与总耗时上升 |
| `CASE_BATCH_CAP` 8 → 3 | `5 → 3` | 强制小批，最稳；适合反复截断的模型 |
| `OUTPUT_BUDGET` 0.60 → 0.90 | `5 → 8`（被 CAP 截） | 余量从 1.67x 掉到 1.11x；**`MAX_TOKENS` 的作用被抵消**，截断率回到原点 |
| `TOKENS_PER_REQ` 1800 → 900 | `5 → 8`（被 CAP 截） | 相当于假设每条需求只输出一半 token；估算偏乐观就会截断 |
| `CASE_BATCH_SIZE=2` | 恒为 2 | 绕过以上全部常数；排障用，不要长期设 |

> 为什么这么设计：截断的根因是「单次要输出的内容超过 `max_tokens`」。
> 只要批次容量与 `max_tokens` 同比例增长，调高上限就完全换不来安全余量 ——
> 必须先把 `OUTPUT_BUDGET` 压到一个有余量的比例，`MAX_TOKENS` 这个旋钮才真正生效。

### 1.3 流式、并发与温度

| 变量 | 默认值 | 作用 | 调它会发生什么 |
| --- | --- | --- | --- |
| `PRD2TC_STREAM` | `1`（开） | 流式输出 | 关闭后走非流式 `_post` 路径（`llm.py:48-66`），实时进度面板只有阶段级百分比。**`--repro` 会强制关闭**（`llm.py:165-168`、`tests/test_realtime.py:176`） |
| `PRD2TC_STREAM_PUSH_MS` | `500` | 实时指标推送间隔（毫秒） | 调小推送更密、UI 更"活"，但增加序列化与网络开销；它是限流阀，不是精度阀（`config.py:41`、`metrics.py` 的 `push_interval_ms`） |
| `PRD2TC_JOBS` | `1` | 默认并发批次数（阶段 1 分段 / 阶段 3 模块并行） | `max(1, 值)`，非法值（`0`、负数）回落到 1 而不是崩溃（`config.py:84`、`tests/test_concurrency_defaults.py:10-13`）。CLI `--jobs` 显式给出时覆盖它；工作台「模块并发数」输入框默认 **4**（`ui/src/pages/Dashboard.tsx:14`）。**实测串行 5 模块约 1013s，并发约 332s**；过高会触发 Provider 限流（429），`--max-retries` 只能缓解不能消除 |
| `PRD2TC_TEMPERATURE` | `0.4` | 全局温度（`Config.temperature`） | 作为分阶段温度的**中间优先级**（见下） |
| `PRD2TC_TEMP_S1` / `S2` / `S3` | 见下 | 分阶段温度覆盖 | 一旦设置，该阶段完全按此值，全局 `PRD2TC_TEMPERATURE` 对它失效（`stages.py:59-67`） |
| `PRD2TC_REPRO` | `0` | 可复现模式 | 三阶段温度**恒为 0**（贪心解码），并强制关闭流式；同时纳入 configHash（`stages.py:64-65`、`pipeline.py:299`） |
| `PRD2TC_SEED` | 未设置 | 采样种子 | 仅 OpenAI 兼容端点生效，Anthropic/MiniMax 忽略（`config.py:19`）。非法内容视为未设置（`config.py:81`） |

温度三档的实际默认值与优先级（`stages.py:53-67`）：

| 阶段 | 内置默认 | 覆盖顺序 |
| --- | --- | --- |
| 1 需求分析（事实抽取） | `0.2` | `PRD2TC_TEMP_S1` → `PRD2TC_TEMPERATURE` → `0.2` |
| 2 测试方案 | `0.3` | `PRD2TC_TEMP_S2` → `PRD2TC_TEMPERATURE` → `0.3` |
| 3 用例生成 | `0.3` | `PRD2TC_TEMP_S3` → `PRD2TC_TEMPERATURE` → `0.3` |

> ⚠ 未验证/已知不一致：`README.md:222` 与 `docs/prompt-gate-optimization.md:248` 仍写着阶段 3 为 `0.5`。
> 源码与提交 `6799acf` 的说明是「阶段 3 与阶段 2 同为 0.3，不再用 0.5」（`stages.py:54-56`）。**以代码为准**。

### 1.4 长文档、缓存与中转

| 变量 | 默认值 | 作用 | 调它会发生什么 |
| --- | --- | --- | --- |
| `PRD2TC_CHUNK_SIZE` | `6000` | 长文档分段字符数 | 调小 → 分段更多、阶段 1 请求更多（可并发），合并成本上升 |
| `PRD2TC_CHUNK_OVERLAP` | `1200` | 分段重叠字符数 | 保证跨段的需求不被切断；调小可能丢需求，调大重复抽取增多 |
| `PRD2TC_PROMPT_CACHE` | `0` | Anthropic 请求带 `cache_control` | 是否真的命中以 usage 回显为准，用 `prd2tc probe-cache` 实测（`cli.py:132-164`）。兼容端点可能不回显该字段 |
| `PRD2TC_MARKDOWN_TRANSIT` | `0` | 阶段 3 改输出 Markdown 表格，再由程序转 JSON | 表格没有引号嵌套，**转义类失败从根上消失**；实测同一份示例库（6 条用例）字符数从 3708 降到 1871（**约省 49.5%**）；**落盘产物仍是 JSON**，下游契约不变（`markdown_transit.py`、`stages.py:106-123`）。等价 CLI 开关 `--markdown-transit` |
| `PRD2TC_EXAMPLES` | 未设置 | 示例库目录 | 等价 `--examples`；按与当前 PRD 的相关性检索 Top-K 注入（`cli.py:29-33`） |

## 2. Provider 配置示例与自检

### 2.1 OpenAI 兼容（含 DeepSeek / MiniMax 等）

```bash
export PRD2TC_LLM_PROVIDER=openai
export PRD2TC_API_BASE=https://api.deepseek.com        # 或 https://api.minimax.chat/v1
export PRD2TC_API_KEY=sk-xxx
export PRD2TC_MODEL=deepseek-chat
prd2tc check-llm
```

### 2.2 Anthropic

```bash
export PRD2TC_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-xxx
export PRD2TC_MODEL=claude-sonnet-4-5
prd2tc check-llm
```

### 2.3 本地 Ollama（不需要 Key）

```bash
export PRD2TC_LLM_PROVIDER=ollama
export PRD2TC_API_BASE=http://localhost:11434
export PRD2TC_MODEL=qwen3:8b
prd2tc check-llm
```

### 2.4 `check-llm` 的用途与它能分辨什么

`check-llm` 发一次**最小请求**（`max_tokens=1`，尽量避免计费），把「运行 1 秒后一句无信息量的失败」变成「配置时明确的原因」（`llm.py:430`、`llm.py:466-484`）。

```bash
prd2tc check-llm [--provider openai|anthropic|ollama] [--api-base URL] [--model M] [--api-key K]
```

它返回结构化结论 `ProbeResult`（`llm.py:435`），`kind` 的取值与含义：

| `kind` | 触发条件 | 源码 |
| --- | --- | --- |
| `ok` | 端点返回合法 JSON 且状态正常 | `llm.py:574-579` |
| `config` | `api_base` 为空、缺 `http(s)://` 前缀、或需要 Key 但为空 | `llm.py:530-541` |
| `endpoint` | 返回 HTML 网页 / 非 JSON / HTTP 404 | `llm.py:562-570`、`584-590`、`595-600` |
| `auth` | HTTP 401 / 403 | `llm.py:591-594` |
| `model` | HTTP 400 且响应体含 `model` | `llm.py:604-606` |
| `rate_limit` | HTTP 429 | `llm.py:601-603` |
| `server` | 其它 ≥400 状态 | `llm.py:571-573`、`610-611` |
| `network` | 无法连接 / 超时 | `llm.py:612-617` |
| `malformed` | 探测自身抛异常（兜底，绝不阻断保存） | `llm.py:618-619` |

两个特别有用的提示：

1. **地址漏写 `/v1`**：`_suggest_base` 会算出建议地址并在输出里打印 `建议改为: …/v1`（`llm.py:512-517`、`cli.py:180-181`）。
2. **地址填成了官网/控制台**：识别到响应是 HTML 时直接点明「该地址返回的是网页，不是 API 响应」（`llm.py:497-509`）。

工作台在「模型配置」保存前会真实探测一次端点，不通过则拒绝保存。

### 2.5 三个 Provider 的行为差异

| 维度 | openai（含兼容端点） | anthropic | ollama |
| --- | --- | --- | --- |
| 是否需要 Key | 是 | 是 | 否（`requires_api_key` 返回 False，`config.py:46-47`） |
| 探测路径 | `/chat/completions` | `/v1/messages` | `/api/chat` |
| 流式协议 | SSE，`data:` 行 | SSE，`message_start`/`content_block_delta`/`message_delta` | NDJSON，每行一个 JSON |
| usage 缺失时 | 按字符估算并标 `estimated`（CJK 1 字符≈1 token，ASCII 4 字符≈1 token，`llm.py:121-129`、`llm.py:170-174`） | 分散在 `message_start` 与 `message_delta` | `prompt_eval_count` / `eval_count` |
| `PRD2TC_SEED` | 生效 | 忽略 | 忽略 |
| `PRD2TC_PROMPT_CACHE` | 无效 | 请求带 `cache_control` | 无效 |
| 截断标记 | `finish_reason=length` | `stop_reason=max_tokens`（`LLMResult.truncated` 同时认这两个值，`llm.py:37-39`） | `done_reason=length` |
| 重试 | 只在首 token 之前对 `{429,500,502,503,504}` 重试 | 同 | 同 |

### 2.6 一份可直接复制的配置模板

```bash
# ---- 必填 ----
export PRD2TC_LLM_PROVIDER=openai
export PRD2TC_API_BASE=https://api.deepseek.com
export PRD2TC_API_KEY=sk-xxx
export PRD2TC_MODEL=deepseek-chat

# ---- 预算与批次（默认值即可用；截断频繁时按 §1.2 的表调整）----
export PRD2TC_MAX_TOKENS=16384
export PRD2TC_TOKENS_PER_REQ=1800
export PRD2TC_CASE_BATCH_CAP=8
export PRD2TC_CASE_OUTPUT_BUDGET=0.60

# ---- 性能 ----
export PRD2TC_JOBS=4              # 过高会触发 Provider 限流
export PRD2TC_STREAM=1
export PRD2TC_STREAM_PUSH_MS=500

# ---- 稳定性（反复遇到转义类失败时打开）----
export PRD2TC_MARKDOWN_TRANSIT=1

# ---- 回归验收基线 ----
# export PRD2TC_REPRO=1           # 温度归零 + 强制关闭流式
```

### 2.7 CLI 子命令总览

| 命令 | 用途 |
| --- | --- |
| `generate` | 三阶段生成（本文档的主角） |
| `check-llm` | 保存配置前自检：探测端点可用性（§2.4） |
| `probe-cache` | 探测 Provider 的 Prompt 缓存是否生效（连发两次相同 system 前缀） |
| `prompts` | 打印各提示词资产的版本号与内容哈希（判断"跑的是哪一版"） |
| `validate` | 校验用例 JSON（id 唯一、字段与枚举、追溯格式） |
| `stats` | 用例统计（总数/模块/优先级/标签/REQ 覆盖） |
| `export` | 从已有用例目录导出 Excel / XMind |
| `init` | 只创建输出骨架与清单，不调模型 |
| `diff` | 需求版本差分（改了 PRD 后影响哪些需求） |
| `merge` | 增量合并新旧两轮的用例 |
| `import-results` | 导入测试执行结果（回写 `lastExecution`） |
| `promote-examples` | 把人工评审且执行通过的用例升级为示例库条目（必须显式 `--reviewed`） |
| `compare-runs` | 结构级复现对拍（`--fail-on-drift` 用于 CI 卡口） |

## 3. 常见故障排查表

### 3.1 「未配置 LLM 凭证」，但用户明明配了

| 项 | 内容 |
| --- | --- |
| 症状 | 工作台报「未配置 LLM 凭证」，重新保存模型配置后仍然失败 |
| 根因 | **会话已过期**。`store.sessions` 是**服务端内存态**，TTL 1 小时，且随服务重启全部失效（`service.py:152`、`service.py:217-221`）。用户重新保存得到的是**新** `sessionId`，而 task.json 里存的是**旧** ID；端点若仍拿旧 ID 去取会话就会取不到 → 静默退化成空凭证 → 报「未配置」（`service.py:317-339`） |
| 处置 | 1) 先在「模型配置」重新保存会话；2) 回到运行页点「恢复/重试」——它会把**当前** `sessionId` 一起带上（`service.py:877-887`）。已完成阶段的产物不会丢失 |

为什么提示必须区分「过期」与「从未配置」：写错的话用户会反复去「模型配置」重新保存却始终失败，因为真因是旧会话失效、而新会话没被用上（`service.py:86-105`）。

因此 `_credential_error` 是**纯函数**且被单测锁定——stub 模式下 `run_task` 走不到该分支，若内联在 `run_task` 里这两条消息**无法被任何测试触达**（变异测试曾因此漏报，`tests/test_service_api.py:843-873`）。

**三个变更端点必须由前端带上当前 `sessionId`**：

| 端点 | 为什么必须带 | 源码 |
| --- | --- | --- |
| `POST /api/v1/runs/{id}/stages/{stage}/approve` | 批准后要立刻启动**下一阶段**，用的是新会话 | `service.py:750-766` |
| `POST /api/v1/runs/{id}/modules/{module}/retry` | 重试失败模块 | `service.py:777-786` |
| `POST /api/v1/runs/{id}/resume` | 恢复常已隔了一段时间、甚至服务重启过，旧会话多半失效；采用新会话后会**写回任务**供后续阶段继续使用 | `service.py:877-887` |

`_resolve_session` 的规则：**优先用本次请求带来的会话**；不带时回退到 task.json 里记录的那个；`provided` 无效时直接 401 明确报错，**不静默降级**（`service.py:328-335`）。

`sessionId` 是控制参数，必须在 `approve` 里先从草稿体里摘掉——`save_draft` 会对整个 body 做哈希并原样存为草稿内容，混进去会污染草稿与 `contentHash`（`service.py:755-759`）。

### 3.2 把官网/控制台地址当成 API 地址

| 项 | 内容 |
| --- | --- |
| 症状 | 运行开始后只得到一句 `HTTP 404: ...`，看不出原因 |
| 根因 | `api_base` 填成了 `https://platform.minimax.chat` 这类控制台/官网首页。它对任何 POST 都回 404（或 200 的 HTML 页面） |
| 为什么难查 | 原始 `HTTPError` 只给一句 4xx；如果直接把它灌进日志，是一页几百字符的 HTML，既看不出原因也淹没其它错误 |
| 处置 | 1) 跑 `prd2tc check-llm`：它会识别 HTML 响应并直接输出「地址返回的是网页而非 API 响应（<title>）」，同时提示「请填写 API 接口地址」（`llm.py:497-509`、`llm.py:584-590`）；2) 到 Provider 控制台复制**OpenAI 兼容**的接口地址（通常以 `/v1` 结尾）；3) 若只是漏了 `/v1`，输出里会直接给出 `建议改为: …` |

### 3.3 门禁失败

门禁是「三层约束」的最后一层（prompt 自检 → 机器门禁 → 错误反馈重出，`gates.py:1-9`）。默认每阶段最多反馈修正 2 次（`--max-gate-retries 2`），最终仍不过则整条流水线失败、退出码 1。

**先判断根因类型，不要默认修改 PRD**（`pipeline.py:496-501` 的处置建议即以此开头）：

| 根因 | 典型错误文案 | 处置 |
| --- | --- | --- |
| **PRD 事实缺失** | `需求分析遗漏需求点 REQ-X-001（PRD 中存在但报告未覆盖）`；或某章节反复被要求补而 PRD 里确实没有 | 补 PRD 对应章节后**重新分析**（`--stage 1`）。这是唯一应该改 PRD 的情况 |
| **模型输出不完整** | `缺少必备章节「风险与疑点」`；`未生成任何用例`；`输出被长度截断（finish_reason=length）` | 先重试失败模块；反复失败则降低单次输出量（调小 `PRD2TC_CASE_BATCH_CAP`、调高 `PRD2TC_CASE_OUTPUT_BUDGET` 之外的余量、或开 `--markdown-transit`） |
| **数据契约错误** | `[用例#3 TC-X-001] testData 必须是「键=值」字符串数组`；`预期不可验证（空洞断言）` | 属产物格式问题，不是 PRD 问题。重试即可；若系统性复现，检查提示词与 `validator` 是否同源（`stages.py:126-154`、`validator.py:121-149`） |
| **Provider / 环境问题** | `HTTP 429 触发限流`；`无法连接 …`；`未配置 LLM 凭证` | 见 §3.1、§3.2；限流时降低 `--jobs` |

各阶段门禁项目（`gates.py:21-36`、`gates.py:43-92`）：

- 阶段 1：六个必备章节（功能模块清单/角色清单/需求点明细/业务规则/非功能需求/风险与疑点）+ 与 PRD 的 REQ 覆盖**双向**对齐（遗漏与臆造都报错）。
- 阶段 2：八个必备章节。
- 阶段 3：JSON（或表格）可解析 + 字段强校验 + 批内重复检测 + **每条用例至少一个断言动作**。
- 阶段 3 收尾还会做覆盖率校验（`traceability.validate_traceability`），失败时用例**不丢弃**（见 §3.4）。

三层门禁开关（`cli.py:294`）：`--max-gate-retries N` / `--allow-gate-fail`（仅告警）/ `--no-gate`（退回纯 prompt 自检）。

### 3.4 输出被截断

| 项 | 内容 |
| --- | --- |
| 症状 | 门禁报「模型输出被长度截断（finish_reason=length）」或「输出预算被思考过程耗尽（正文为空）」 |
| 为什么**无条件**报错 | 截断是**独立于内容错误**的根因。实测故障（运行 `9d97060e`）：输出被 `max_tokens` 截断后，`extract_cases` 的「修补截断 JSON」回退会 `rfind("]")` 找到用例内部 `tags`/`requirement` 的 `]`，从而修补出一个**合法但残缺**的数组 → 门禁只看修补后的部分 → 零错误 → 模块被判 succeeded、检查点只存下残缺用例，**直到最后 traceability 才发现 P0 未覆盖**。当时 AGENT 只产出 1 条（6 条需求）、SCENE 只 2 条（7 条需求）。因此代码刻意不看 `errors` 是否为空（`stages.py:1358-1384`） |
| 自愈机制 | 截断触发**批次对半拆分**重试：`_split_batch_if_budget_exhausted` 把该批一分为二重新排队，最多 2 层（`stages.py:1263-1297`）。触发条件刻意放宽为**任何** `finish_reason=length`（不再限于「思考耗尽」），因为正确杠杆都是降低单次输出量 —— 原地重试同样大的批次几乎必然再次截断 |
| 处置 | 若拆分 2 层后仍截断：调高 `PRD2TC_MAX_TOKENS`（配合保持 `OUTPUT_BUDGET=0.60` 才有意义）、调小 `PRD2TC_CASE_BATCH_CAP`，或开 `--markdown-transit`（表格不含引号嵌套，输出体积显著更小） |

思考耗尽（`looks_like_reasoning_exhausted`）是另一种形态：未闭合的 `<think>` + 正文为空，说明推理占满了全部输出长度。诊断文案会明确要求「直接输出 JSON 数组，禁止输出 `<think>`」（`stages.py:1370-1377`、`reasoning.py:127`）。

### 3.5 「输入不存在」

| 项 | 内容 |
| --- | --- |
| 症状 | 阶段 2/3 报 `输入不存在: [WindowsPath('D:/a.md'), WindowsPath('D:/b.png')]` |
| 根因 | 文件夹上传后 `request.prd` 是**列表**，而 `request_dict` 曾写成 `str(req.prd)`，把列表字符串化成 `"[WindowsPath('...'), WindowsPath('...')]"`。阶段 1 用内存里的原始请求故能成功；阶段 2/3 从 `task.json` 重建请求时，这串文本被当成**单个路径**（`service.py:287-294`，事故见提交 `a9eebc7`） |
| 如何识别 | 报错里的路径以 `[` 开头且含 `WindowsPath(`/`PosixPath(` —— 这不可能是真实路径 |
| 现状 | 已修：`_encode_prd` 序列化为字符串列表、`_decode_prd` 反序列化还原成 `Path` 列表，**并兼容旧格式**（正则还原 `WindowsPath('...')`），好让**已经失败的任务仍能恢复**、不必重新上传（`service.py:40-65`） |
| 该报错还会在哪出现 | 单人文件路径确实不存在时（`ingest.py:654-655`）。此时 `ingest` 会把所有被跳过输入的告警带进异常，并**逐条截断到 160 字符**，避免整页路径列表淹没原因（`ingest.py:730-738`） |

### 3.6 阶段无法开始：产物完备性检查

原先「能否从中间阶段开始跑」只有散落的 `.exists()`，会放过三类**静默错误结果**（`artifacts.py:1-21`）。现在进入阶段前统一做 preflight，失败时给出可操作建议。

**五类问题码**（`artifacts.py:91`）：

| `code` | 含义 | 判据 |
| --- | --- | --- |
| `missing` | 产物不存在 | glob 无匹配文件（`artifacts.py:109-112`） |
| `empty` | 产物为空或过小 | 小于该产物的 `min_bytes` 下限；纯空白也算（`artifacts.py:114-120`） |
| `malformed` | JSON 无法解析 | 被截断或写坏（`artifacts.py:122-129`） |
| `incomplete` | 结构不完备 | `require_field` 为空 —— 例如 `requirements.json` 里 `requirements: []`（`artifacts.py:130-136`） |
| `stale` | 陈旧产物 | 清单记录的 PRD 哈希与当前不一致（`artifacts.py:168-176`） |

产物契约（`artifacts.py:54-84`）：

| 产物 | 路径 | 下限 | 结构要求 | 由哪阶段产出 | 哪些阶段依赖 |
| --- | --- | --- | --- | --- | --- |
| 需求分析报告 | `analysis/需求分析报告.md` | 80B | — | 1 | 2 |
| 结构化需求 | `analysis/requirements.json` | 2B | `requirements` 非空 | 1 | 2、3 |
| 测试方案 | `plan/测试方案.md` | 80B | — | 2 | 3 |
| 用例集 | `cases/cases/*.json`（glob） | 2B | — | 3 | — |

**为什么必须拒绝「用另一份 PRD 生成的上游产物」**：这是最危险的一类静默错配 —— 改了 PRD 后直接 `--stage 3`，会用**旧 plan** 生成与当前 PRD 不匹配的用例，且毫无提示（`artifacts.py:168-176`）。

报错文案里的三个开关（`artifacts.py:195-207`）：

| 参数 | 含义 |
| --- | --- |
| `--stage N` | 只跑阶段 N，**复用**已有上游产物 |
| `--from-stage N` | 从阶段 N **补跑**到最后一阶段（缺上游时用这个） |
| `--allow-stale-artifacts` | 显式放行陈旧产物（默认禁止） |

一个实现上的关键细节：**陈旧检测必须在 `init_output` 之前采样 PRD 哈希**。`init_output`（`resume=False` 时调用）会重写清单，抹掉「已有产物属于哪份 PRD」的记录；若在其后才读，拿到的永远是刚写入的新哈希，陈旧检测形同虚设（`pipeline.py:237-240`、`artifacts.py:155-164`）。

preflight 只校验**本次将执行**阶段的输入，且跳过「由本次运行的上游阶段产出」的输入 —— 否则 `--stage all` 会因阶段 1 尚未生成而误报（`pipeline.py:374-392`）。

### 3.7 运行很慢 / 被限流（HTTP 429）

| 项 | 内容 |
| --- | --- |
| 症状 | 单个模块长时间无产出；错误里出现 `HTTP 429 触发限流` |
| 根因 | `--jobs` 过高。并发确实有效（实测串行 5 模块约 1013s，并发约 332s），但 Provider 侧的速率限制是硬约束 |
| 判据 | 限流的 `kind` 是 `rate_limit`：**端点可达、鉴权通过、但当前被限流**（`llm.py:601-603`）。若 `kind` 是 `auth`，那是密钥问题，不是限流 |
| 处置 | 把「模块并发数」从 4 降到 2 或 1；`PRD2TC_MAX_RETRIES`（默认 3，指数退避 2/4/8 秒）只缓解不能消除限流 |
| 注意 | 已产出内容后遇到 5xx **不会重试**（`llm.py:98-103`）—— "重试次数调大"对长输出帮助有限，正确杠杆是调小批次 |

### 3.8 运行卡住了吗？看「静默时长」

实时指标里的**静默时长**是判断"是否卡住"的直接证据：思考型模型会把大量 token 花在 `<think>` 里，
期间界面上看不到正文，但 token 数仍在涨。

| 观察 | 判读 |
| --- | --- |
| 首 Token 延迟正常、token 持续增长、静默时长为 0 | 正常产出中 |
| token 增长、静默时长同步增长、正文不动 | 模型在思考中（`reasoning` 单独统计，绝不混入 `content`，`llm.py:121-138`） |
| token **不增长**且静默时长持续增长 | 真卡住：取消后调小批次或降低并发重试 |
| 想要一帧不漏的收尾快照 | WebSocket 在终态时**强制**推送最后一帧指标再关闭（`service.py:996-1005`）；否则短运行会在连接建立前就结束，实时面板永远空着 |

取消是**真止损**：流式回调每行检查取消标志，命中即抛 `LLMCancelled` 停止接收（不再计费；已产出内容的部分不重试，
避免重复拼接，`llm.py:86-94`、`llm.py:150-153`）。

### 3.9 吞掉异常的"降级路径"（为什么产物少了一部分却报成功）

有几个环节刻意**只告警不中断**，产物缺失时不会让整条运行失败。若发现某个副产物没生成，按此表定位：

| 缺失的产物 | 原因 | 位置 |
| --- | --- | --- |
| `analysis/ui-spec.json` | 原型附件解析失败（原型是增强输入，不是硬依赖） | `warnings`，`pipeline.py:129-131` |
| `analysis/image-spec.json` | 图片分析失败或没有图片引用 | `warnings`，`pipeline.py:203-212` |
| `analysis/prd-bundle.json` | 摄取留痕写入失败（如目录未就绪） | `warnings`，`pipeline.py:274-275` |
| `cases/*.xlsx` / `.xmind` | 导出失败**不得**阻断主流程，只降级为告警 | `warnings`，`pipeline.py:504-514` |

反过来，**必须失败**的情形不要当成降级：门禁未通过（除非 `--allow-gate-fail`）、上游产物不完备、
未配置凭证、PRD 无可用内容（`pipeline.py:233-234`）。

### 3.10 阶段 3 全部/部分模块失败

先看 `run-state.json` 的 `modules`：它标出每个模块是 `succeeded` / `failed` / `skipped`（`pipeline.py:450-462`）。
再看 `cases/.failures/<MODULE>.txt` —— 里面是**该模块最后一次的原始模型输出**，是判断"模型到底吐了什么"最直接的证据
（`pipeline.py:463-465`）。常见组合：

| `modules` 表现 | 根因方向 |
| --- | --- |
| 全部 `failed`，失败样本里是残缺 JSON | 批次过大 → 截断，见 §3.4 |
| 全部 `failed`，失败样本里是 Markdown 表格 | 默认模式应输出 JSON；若确实想走表格，显式开 `--markdown-transit` |
| 个别模块 `failed`，其余 `succeeded` | 该模块需求数多或描述复杂；用工作台的「重试」按模块重跑即可（`service.py:777-786`） |
| `modules` 为空且 `stage` 停在 `analysis` | 失败发生在阶段 1/2，看 `run-manifest.json` 的 `stages.*.status` 与 `error` |

### 3.11 清单、指纹与"哪些文件算产物"

- `run-manifest.json` 的 `artifacts` 字段是 `artifact_inventory(root)` 的结果：**运行根目录下所有文件的相对路径 + sha256**（`io.py:155-163`）。它刻意**排除** `run-manifest.json`、`manifest.json`、`run-state.json`，以及任何位于 `cases/.checkpoints/` 下的文件 —— 否则清单会包含它自己，写入即改变自身哈希。
- **两个哈希不可混用**（`pipeline.py:244-252`）：
  - `prdHash` = **正文文本**哈希，是已发布契约 —— `cmd_diff` 用它判断"新 PRD 是否与历史运行同一份"。掺入资产指纹会让它永不匹配，导致 diff 把未改动的 PRD 误判为 modified。
  - `inputHash` = 正文 + 资产（含图片内容），`--resume` 用它 —— 换了附件/图片就会让旧检查点失效。
- 服务模式的工作区（默认 `.prd2tc-workspace/`，由 `create_app(state_dir=…)` 决定）与 `output/<product>/<version>/` 是**两套**目录：前者放上传的 PRD、任务 JSON、事件流、草稿；后者才是生成产物。`.gitignore` 对两者都做了忽略。
- 重跑某阶段时会**丢弃该阶段及其下游**的产物（`analysis` → 丢弃 analysis/plan/cases；`plan` → 丢弃 plan/cases；`cases` → 丢弃用例、检查点、失败样本、追溯矩阵与导出文件），上游产物保持不变，因此不重新消耗上游 token（`service.py:796-854`）。界面上的 `discarded` 列表就是被删掉的文件清单。

## 4. 分段执行与省时

```bash
prd2tc generate --prd PRD.md --stage all            # 三阶段全跑（默认）
prd2tc generate --prd PRD.md --stage 3              # 只跑阶段 3，复用已有分析与方案
prd2tc generate --prd PRD.md --from-stage 2         # 从阶段 2 跑到最后（补方案 + 用例）
prd2tc generate --prd PRD.md --stage 3 --allow-stale-artifacts   # 显式复用旧上游产物
```

语义差异（`artifacts.py:214-233`）：

| 参数 | 会真正执行 | 典型用法 |
| --- | --- | --- |
| `--stage all` | 1 → 2 → 3 | 首次生成；改了 PRD 后的完整重跑 |
| `--stage N` | 只有 N | **只重跑用例**（`--stage 3`）：分析报告与测试方案都没问题、只想换一批用例。不会重新消耗上游 token |
| `--from-stage N` | N → … → 3 | 上游缺了或想连下游一起重做：如 `--from-stage 2` 补测试方案并重出用例 |

`--stage 3` 的常见用法（不改 PRD、复用已有分析/方案）现在**安全且快速**：不必每次重新生成 plan（提交 `6799acf` 的结论）。

配套的 `--resume` / `--force` 是**另一层**机制，与分段执行正交：

- `--resume`：复用 `cases/.checkpoints/`，前提是 inputHash / promptHash / model / configHash 四项指纹全一致（`pipeline.py:320-323`）。
- `--force`：明确放弃旧检查点。
- configHash 纳入了 `maxTokens`、原型契约哈希、图片结论哈希与摄取结果哈希 —— **改了原型、换了图片、调了 `max_tokens` 都会让旧检查点失效**（`pipeline.py:296-313`）。

## 5. 服务模式运维注意

```bash
pip install -e '.[service]'
uvicorn 'prd2tc.service:create_app' --factory --host 127.0.0.1 --port 8000
```

1. **会话是内存态**。API Key 只保留在服务进程内存中（`WorkspaceStore.sessions`，`service.py:152`），TTL 1 小时（`put_session(ttl=3600)`，`service.py:217-218`），**服务重启后全部失效，必须重新保存**。这是 §3.1 的根因来源。
2. **`ui/dist` 静态托管**。生产部署先 `cd ui && npm run build`，产物交给 `create_app(ui_dir="ui/dist")`；不传 `ui_dir` 时默认找 `<仓库>/ui/dist`（`service.py:1025`）。挂载了 `/assets`，其余路径走 SPA fallback 到 `index.html`（`service.py:1029-1035`）。
3. **WebSocket 需要 `websockets` 包**。`pyproject.toml:23-25` 的 `service` extra 显式包含它。缺少时 uvicorn 无法完成 WebSocket 升级，实时推送会**静默退化**为轮询 —— 前端 `hooks.ts` 始终并行开着一条 `setInterval(load, 10000)`，WS 断开后以指数退避重连（上限 10s），因此退化后表现为「10 秒才刷新一次」（`ui/src/hooks.ts:33-36`、`README.md:77-80`）。
4. **任务状态不只在内存**。任务 JSON 落在工作区 `tasks/`，事件落 `events/<runId>.jsonl`；服务启动时会把上次残留的 `pending`/`running` 任务标记为 `failed`，错误为 `service restarted before task completion`（`service.py:157-162`）。WebSocket 不是唯一事实源。
5. **取消要真止损**。`POST /api/v1/runs/{id}/cancel` 置取消标志；流式回调逐块检查，命中即停止接收响应（`pipeline.py:215-218`、`service.py:787-793`）。进度回调里也做了保护：已取消的任务不得把状态改回 `running`（`service.py:366-369`）。
6. **上传上限**：单文件 64MB、单次总计 256MB（`service.py:23-24`）。
7. **运行中读产物可能遇到瞬时 `PermissionError`**。Windows 上 `os.replace` 原子替换时**读方**也会拿到共享冲突；`WorkspaceStore._json` 因此对「文件正被替换」这一合法瞬时状态重试 6 次，重试仍失败则**返回默认值而不抛错**（读不到辅助产物不应让整个请求 500，`service.py:168-196`）。

## 6. 产物目录说明

```
output/<product>/<version>/
├── run-manifest.json          # 权威运行清单（见下）
├── manifest.json              # 同一内容的兼容副本（旧工具读取）
├── run-state.json             # 运行状态与指纹（resume 判据）
├── reproduce.json             # 结构指纹（可复现性契约，排除自由文本）
├── analysis/
│   ├── 需求分析报告.md          # 阶段 1 正文（阶段 2 的输入）
│   ├── requirements.json      # 结构化需求 + 模块清单（阶段 2/3 的输入）
│   ├── prd-bundle.json        # 摄取留痕：正文与资产来自哪些文件（含告警与内容指纹）
│   ├── ui-spec.json           # 原型 UI 契约真值（页面/页签/真实标识符/文案/路由）
│   ├── image-spec.json        # PRD 图片的视觉分析结论
│   └── .image-cache/          # 图片视觉分析缓存（按内容 key 命中）
├── plan/
│   └── 测试方案.md             # 阶段 2 产物（阶段 3 的输入）
└── cases/
    ├── cases/<MODULE>.json    # **机读源**：按模块拆分的用例数组
    ├── 用例集.md               # 评审稿（统计 + 追溯表 + 按模块明细）
    ├── traceability.json      # REQ ↔ TC 覆盖矩阵
    ├── <product>-<version>-用例集.xlsx    # 自动导出（Excel）
    ├── <product>-<version>-用例集.xmind   # 自动导出（XMind）
    ├── .checkpoints/<MODULE>.json         # `--resume` 用的模块检查点
    ├── .failures/<MODULE>.txt             # 失败模块的原始模型输出（便于定位）
    └── partial/                           # 门禁未通过时保留的用例（**勿当正式交付**）
        └── READ-ME-部分用例未通过门禁.md
```

各文件要点：

- **`run-manifest.json`**（权威，`io.py:18`）：`schemaVersion`、`toolVersion`、`runId`、`prdHash`、`bundleHash`、`model`、`parameters`（provider/apiBase/temperature/timeout/maxRetries/jobs/maxTokens/profile/minCasesPerRequirement/repro/seed）、`prompts`（每个提示词资产的文件名 → `prompt vX.Y.Z` 版本 + sha256）、`examples`、`attachments`、`stages`（每阶段的 model/tokens/durationMs/retries/status/gate.errors，阶段 3 另有 `moduleTokens` 与 `exports`）、`artifacts`（全部产物的相对路径 + sha256）、`status`。
  CLI 结束时会打印其中的 token 账单（含门禁重试累计值与 Prompt 缓存命中量，`cli.py:109-130`）。
- **`manifest.json`**：与 `run-manifest.json` 字节内容相同的兼容副本；`update_manifest` 每次都同时写两份（`io.py:71-80`）。注意 `artifacts.LEGACY_MANIFEST_NAME` 与 `io.LEGACY_MANIFEST_NAME` 的常量值**指向不同文件**（前者 `manifest.json`，后者 `run-manifest.json`），读清单时两处都用「新名优先、旧名兜底」的方式处理。
- **`run-state.json`**：`runId`、`inputHash`、`promptHash`、`configHash`、`model`、`status`、`stage`、`modules`（模块 → `succeeded`/`failed`/`skipped`）。它是 `--resume` 的判据，也是「哪些模块失败」的查询点（`io.py:97`）。
- **`reproduce.json`**：`run_fingerprint(root)` 的结构指纹，`prd2tc compare-runs` 用它做对拍（`pipeline.py:518-521`）。
- **`analysis/prd-bundle.json`**：摄取结果的结构化留痕 —— 让「正文与资产是从哪些文件来的」可追溯（`pipeline.py:268-275`）。
- **`analysis/ui-spec.json`**：原型（HTML/JS 交互式设计产物）解析出的 UI 契约真值。**它的哈希进入 configHash**，改了原型必须重新生成（`pipeline.py:256-262`、`pipeline.py:302-305`）。
- **`analysis/image-spec.json`**：图片视觉结论；同样进入 configHash（`pipeline.py:306-309`）。仅当真的有图片引用时才产出（`pipeline.py:206-212`）。
- **`cases/.checkpoints/`**：模块级检查点。`--resume` 命中且模块状态为 `succeeded`/`skipped` 时直接复用（`pipeline.py:459-462`）。注意它**不计入** `artifacts` 清单（`io.py:159`）。
- **`cases/partial/`**：门禁失败时把**已生成**的用例保留在这里，并附 `READ-ME-部分用例未通过门禁.md` 列出错误与处置建议。正式 `cases/cases/` 保持为空。为什么要有它：实测运行 `9d97060e` 已产出 43 条，却因最后一步覆盖率不过而**全部消失**，用户只看到「失败」与空目录（`pipeline.py:484-502`）。
- **`cases/traceability.json`**：REQ ↔ TC 覆盖矩阵，与 `用例集.md` 的追溯表同源（`io.py:151-152`）。

## 7. 快速诊断清单

| 现象 | 先看这里 |
| --- | --- |
| 任何配置类失败 | `prd2tc check-llm`（§2.4） |
| 提示词缓存是否真的生效 | `prd2tc probe-cache`（连发两次相同 system，观察 `cache_read`/`cache_creation` 回显，`cli.py:132-164`） |
| 用的是哪一版提示词 | `prd2tc prompts`（打印每个资产的 `prompt vX.Y.Z` 与 sha256 前 12 位，`cli.py:219-223`） |
| 产物是否合法 | `prd2tc validate --dir output/<p>/<v>/cases/cases` |
| 规模与覆盖 | `prd2tc stats --dir output/<p>/<v>/cases/cases` |
| 改了 PRD，影响哪些需求 | `prd2tc diff --prd PRD.md --from output/<p>/<旧版本>` |
| 两次运行是否真的复现 | `prd2tc compare-runs <RUN_A> <RUN_B> [--fail-on-drift]` |
| 门禁为什么失败 | 运行详情的 `stages.*.gate.errors`、`run-state.json` 的 `modules`、`cases/.failures/*.txt` |
| 实时面板不动 | 确认 `websockets` 已安装（否则退化为 10s 轮询）；看「静默时长」指标（§3.8） |
| 少了一个副产物 | 查 §3.9 的降级路径表 —— 多数副产物缺失只告警不中断 |

## 附录：环境变量速查（按源码 `os.environ` 出现顺序）

| 变量 | 读取位置 |
| --- | --- |
| `PRD2TC_LLM_PROVIDER` | `config.py:51` |
| `PRD2TC_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` | `config.py:56`、`config.py:60-63` |
| `PRD2TC_API_BASE` / `OPENAI_BASE_URL` | `config.py:64-66` |
| `PRD2TC_MODEL` | `config.py:70` |
| `PRD2TC_TEMPERATURE` | `config.py:71`、`stages.py:66` |
| `PRD2TC_TIMEOUT` | `config.py:72` |
| `PRD2TC_MAX_RETRIES` | `config.py:73` |
| `PRD2TC_CHUNK_SIZE` / `PRD2TC_CHUNK_OVERLAP` | `config.py:74-75` |
| `PRD2TC_MAX_TOKENS` | `config.py:77` |
| `PRD2TC_PROMPT_CACHE` | `config.py:78-79` |
| `PRD2TC_REPRO` | `config.py:80` |
| `PRD2TC_SEED` | `config.py:81` |
| `PRD2TC_STREAM` / `PRD2TC_STREAM_PUSH_MS` | `config.py:82-83` |
| `PRD2TC_JOBS` | `config.py:84` |
| `PRD2TC_MARKDOWN_TRANSIT` | `config.py:85-86` |
| `PRD2TC_TEMP_S1` / `S2` / `S3` | `stages.py:66` |
| `PRD2TC_CTX_TOP_K` | `stages.py:317` |
| `PRD2TC_TOKENS_PER_REQ` / `PRD2TC_CASE_BATCH_CAP` / `PRD2TC_CASE_OUTPUT_BUDGET` | `stages.py:333-335` |
| `PRD2TC_CASE_BATCH_SIZE` | `stages.py:340`（硬覆盖，排障用） |
| `PRD2TC_EXAMPLES` | `cli.py:29`（等价 `--examples`） |

> 这些模块级常数（`CTX_TOP_K`、`TOKENS_PER_REQ_EST` 等）在**导入时**读取一次环境变量。
> 因此在一个长期存活的进程（如 `uvicorn`）里改环境变量**不会生效**，必须重启服务；
> 工作台走的是「模型配置」会话，它覆盖的是 provider/model/apiBase/apiKey/temperature/timeout，
> 而批次预算类参数只能由服务进程的环境变量决定。

