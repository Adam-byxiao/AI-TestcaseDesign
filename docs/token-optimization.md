# Token 成本优化方案

> 状态：已评审，P0 执行中　日期：2026-02-11
> 背景：实测阶段 1 单次消耗 36,902 tokens（28K 字符 PRD，2 次门禁重试），MiniMax 账单远超预期。

## 1. 问题量化

token 消耗 ≈ `PRD 规模 ×（分段数 × (1+重试次数) + 合并轮次）`——被"重复注入 + 重试全量重发"放大，而非单次分析本身贵。

## 2. 结构性浪费根因（按影响排序）

1. **中间产物重复注入**：测试方案全文在阶段 3 的每个模块×每批都重复发送（15 模块 × 6K 方案 ≈ 90K 纯冗余）。
2. **重试全量重发**：门禁失败重试时整段大输入原样重发 ×2~3。
3. **无 Prompt 缓存**：system + 任务指令 + 测试原理层是稳定前缀，但未按缓存友好方式组织。
4. **无上下文压缩**：阶段间传冗长 Markdown 而非紧凑结构化 JSON；长 PRD 全文切块后重复送入。

## 3. 对"换 Agent 框架"的评估（结论：不采纳）

Agent 框架（LangGraph/LangChain deepagent）解决编排复杂度，但**增加** token 与延迟（ReAct 多轮、工具描述、历史上下文重复）。证据：
- [LangGraph 相比 llm.invoke 的上下文/计算开销](https://forum.langchain.com/t/langgraph-context-or-compuation-performance-issue-comparing-with-llm-invoke/845)
- [microsoft/agent-framework 参数开销 issue](https://github.com/microsoft/agent-framework/issues/1700)

## 4. 开源实践参考

| 做法 | 代表 | 收益 |
| --- | --- | --- |
| Prompt 缓存 | [Anthropic prompt caching](https://spring.io/blog/2025/10/27/spring-ai-anthropic-prompt-caching-blog)、[prefixcash](https://github.com/Isk4R1oT/prefixcash)、[TikTok 降 50%](https://www.xsky.com/about/news/info/id/1012.html) | 输入成本 ~90% |
| Prompt 压缩 | [LLMLingua-2](https://www.npmjs.com/package/@axiomantic/llmlingua-2)、[leanctx（40-60%）](https://github.com/jia-gao/leanctx) | 输入 token 40-60% |
| Agent 长会话上下文管理 | [TokenPilot（60%+）](https://developer.aliyun.com/article/1743287) | 历史压缩淘汰 |
| 长文档 map-reduce + 摘要 | [textsum](https://github.com/pszemraj/textsum/wiki/consolidating-summaries)、[Staged RAG](https://www.mcpworld.com/en/detail/79162fda0e6f65148c8a0468438cf73a) | 避免全文重复 |

## 5. 解决方案（实施顺序）

| 优先级 | 措施 | 说明 | 状态 |
| --- | --- | --- | --- |
| P0-a | **方案移入稳定前缀（system）** | 测试方案/字段约束/用例约束/示例库移入 system，模块 user 输入只保留可变内容（需求 JSON + 检索片段 + 反馈）；前缀字节一致 → 命中缓存 | ✅ 已完成 |
| P0-b | **重试增量修正** | 门禁失败只回传失败用例/章节 + 上次输出（`repair_builder`/`stage3_repair_builder`），不重发 PRD/方案全文 | ✅ 已完成 |
| P0-c | **Prompt 缓存接入** | 已实现 cache_control + usage 回显捕获 + `probe-cache` 探测命令；MiniMax M3 兼容端点走**被动缓存**，官方模型表未列 M3，显式断点不保证生效——以 `usage.cache_read_input_tokens / cache_creation_input_tokens` 回显为准 | 🔍 待用户实跑探测确认 |
| P1-a | **结构化事实源传递** | 阶段间用 requirements.json 精简视图替代冗长 Markdown（`compact_analysis`：需求点明细大表→一行一需求） | ✅ 已完成 |
| P1-b | **上下文压缩** | 四小章合并输入去掉分段报告的 1/3 章（`_compact_partial`） | ✅ 已完成 |
| P2 | **度量先行** | run-manifest 按 阶段/模块 记录 token 明细（`moduleTokens`） | ✅ 已完成 |
| P0-d | **module_context 瘦身** | 用例生成每批的「模块原文上下文」不再携带整份 PRD：按 cfg.chunk_size 分段 + `CTX_TOP_K=2` 只取相关片段（原默认 12000 字符/段导致 28K PRD 全量进每批 ≈5.6K token，实测占 prompt 71%） | ✅ 已完成 |
| P3 | **批次容量自适应** | 批次从固定 8 需求改为按输出预算自适应（`_module_batch_size`：预算 = max_tokens×0.85 ÷ 每条预估 450 token，上限 24 条/批）——小/中模块自动「单模块一次全量」，调用 27→19 | ✅ 已完成 |

**不建议**：换 Agent 框架、盲目调大 max_tokens（推高 completion 成本）。

## 6. 执行记录

- P0-a（已完成）：`stage_3_system()` 稳定 system 前缀；`build_stage_3_user` 仅保留可变内容。
- P0-b（已完成）：`generate_with_feedback` 增加 `repair_builder`；阶段 3 用 `stage3_repair_builder` 增量修正（只回传失败输出 + 错误 + 真实 REQ 清单，不重发方案/完整需求 JSON）。
- P0-d（已完成）：`run_stage_3_modules` 用 `cfg.chunk_size/overlap` 分段（6000）并 `locate(..., top_k=CTX_TOP_K=2)`；模块上下文从 ~30K 字符/批 → ~7~12K 字符/批。
- P3（已完成）：批次容量 `_module_batch_size(cfg, count)` 按输出预算自适应；`PRD2TC_CASE_BATCH_SIZE`（旧参数）硬覆盖兼容，新增 `PRD2TC_CTX_TOP_K / PRD2TC_TOKENS_PER_REQ / PRD2TC_CASE_BATCH_CAP / PRD2TC_CASE_OUTPUT_BUDGET` 可调。
- P0-c（编码已完成，待实跑确认）：`Config.prompt_cache`（env `PRD2TC_PROMPT_CACHE=1`）开启时 Anthropic 请求把 system 包装为带 `cache_control:{"type":"ephemeral"}` 的块；`LLMResult` 新增 `cache_read_input_tokens / cache_creation_input_tokens`，OpenAI 兼容端点取 `prompt_tokens_details.cached_tokens`；`generate_with_feedback` 与 `run_stage_3_modules` 跨轮次/跨批次累积；run-manifest `moduleTokens` 记录 `cacheReadTokens/cacheCreationTokens`（CLI Token 账单同步显示）；新增 `prd2tc probe-cache` 命令连发两次相同 system 前缀，直接观察回显判断缓存是否命中。
  - MiniMax M3 判断依据（用户调研）：M3 在 Anthropic 兼容端点走被动缓存，官方主动缓存模型表未含 M3；`cache_control` 传入不会报错但不保证按显式断点生效，命中与否以 usage 回显为准。
- P1-a（已完成）：`compact_analysis()` 把需求分析报告的「需求点明细」大表替换为一行一需求，阶段 2 输入大幅缩小。
- P1-b（已完成）：`_compact_partial()` 在四小章合并输入中剥离分段报告的第 1/3 章。
- P2（已完成）：`run_stage_3_modules` 增加 `token_callback`，run-manifest 记录 `moduleTokens` 按模块 token 账单。
- 修正（已完成）：`generate_with_feedback` 改为**累积所有调用（含失败重试）的 token**——此前只记最后一次，导致 manifest 的 `tokens` 低估真实消耗（失败重试全量重发的那部分被丢弃）。
- 验证：89/89 测试通过（新增：module 锚定/节别名、P0 module_context 瘦身、P3 批次自适应与覆盖）。
- 实测量化（ecommerce PRD，16 模块 / 150 需求）：module_context 输入 旧 ≈ 27 批 × 30,125 字符 ≈ 153K token → 新 ≈ 19 批 × ≤11,936 字符 ≈ 43K token（≈ -72%）；case 阶段总输入 + completion 估算从 ~280K → ~150K。
- 待办：P0-c Prompt 缓存验证（依赖 Provider 是否支持 cache_control / 前缀缓存）。
