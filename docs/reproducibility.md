# 可复现性方案（已实现，见 §6 交付状态）

> 范围：L1（可复现模式）+ L2（运行指纹与轮间漂移报告）。本稿先讲清定义、变异源与做法，
> 参考开源方案给出理由；§6 为落地记录。

## 1. 先把"复现"定义清楚（避免目标错误）

LLM 生成在 `temperature > 0` + 采样下**逐字一致根本做不到**，也不该作为目标。参考业界做法，
把复现分成三层，逐层定契约：

| 层级 | 定义 | 可行性 | 参考 |
| --- | --- | --- | --- |
| 字节级 | 两次运行输出文件 sha256 完全一致 | 仅当 temp=0 且 Provider 保证贪婪解码；[Azure gpt-35 曾报 temp=0 仍非确定](https://stackoverflow.com/posts/76939860/revisions#1)，OpenAI 官方也说 [seed 也不保证恒定](https://github.com/alexvervloet/openai-api-deep-dive/blob/main/examples/25_seed_determinism.py#1) | 不作为承诺 |
| **结构级** | 模块集合、每模块 REQ 数、每模块用例数、覆盖率、门禁通过性一致 | 可达（下文 L1+L2） | **本方案目标** |
| 语义级 | 用例措辞允许变化，但语义等价（用语义相似度衡量） | 需要 embedding 对比，成本高 | 可选后续项 |

业界同类判断：OpenAI / Anthropic / Google 均无"相同输入必同输出"的承诺；[各厂商一致性对比](https://www.respan.ai/blog/llm_consistency) 指出只能用 temp/seed/结构化输出去逼近，不能承诺。（[vLLM 可复现性文档](https://docs.vllm.com.cn/en/latest/usage/reproducibility/#reproducibility) 也明确：seed+确定性采样只对本地可控推理生效。）

## 2. 变异源盘点（我们 pipeline 里到底什么在变）

| 环节 | 是否确定 | 说明 |
| --- | --- | --- |
| PRD 分段 / chunk id / sourceRefs | ✅ | 同一输入 → 同一分段 |
| 模块锚定 / REQ 顺序编号 / 重编号 | ✅ | 上一轮"模块码锚定"已机器级确定 |
| 模块分组 / 批次切分 | ✅ | `sorted(grouped)`，批次按 `_module_batch_size` 纯函数 |
| 门禁 / 重试状态机 / 失败落盘 | ✅ | 同一内容 → 同一判定 |
| 用例 `module/id/REQ` 一致性 | ✅ | `renumber` 确定性强制 |
| **LLM 内容** | ❌ | 温度 分析0.2/方案0.3/用例0.5 + Provider 采样，唯一内在变异源 |
| 模型别名/灰度（次要） | ⚠️ | 同一 model 名后端可能被 Provider 灰度到不同权重；用 usage.model / system_fingerprint 记录可观测 |

结论：**变异性已经收敛到"LLM 文本"一个自由度**——这是上一轮结构加固的成果，也意味着"结构级复现"是可及的。

## 3. L1 · 可复现模式（repro）

思路：把采样自由度压到最低，作为"验收/回归基线"运行方式。

- 做法：新增 `--repro`（CLI）与 `PRD2TC_REPRO=1`（服务/环境），等价于
  - 三阶段温度归零 `PRD2TC_TEMP_S1/S2/S3=0`（事实抽取本来就该用低/零温）；
  - 关闭与采样相关的非必要随机（我们本来就没有随机：示例检索/批次/并发顺序已确定，只差温度）。
- 记录可观测性到 run-manifest `parameters`：`temperature`（已有）+ 新增 `seed`（若 Provider 支持，如 OpenAI `seed`）、响应里的 `usage.model` / `system_fingerprint`（OpenAI 类），用于区分"我们没变，是后端灰度变了"。
- 契约：**repro 模式下，两次运行要求结构指纹一致**；字节一致不承诺（[厂商差异参考](https://www.respan.ai/blog/llm_consistency)、[OpenAI seed 实测](https://github.com/alexvervloet/openai-api-deep-dive/blob/main/examples/25_seed_determinism.py#1)）。
- 定位：日常探索/迭代用默认温度；**回归验收、CI、给下游 02 自动化跑用例时一律 repro 模式**。

## 4. L2 · 运行指纹 + 轮间漂移报告

思路：既然文本不能逐字一致，就把"该一致的东西"做成可度量的契约，并用结构化 diff 工具呈现。

- **运行指纹**（固化到 manifest 或独立 `reproduce.json`）：
  `prdHash | promptHash | configHash`（已有）+ `modules 集合` + `每模块 REQ 数` + `每模块用例数` + `覆盖率(TC→REQ, REQ→TC)` + `各阶段门禁结果`。
  指纹**排除自由文本**，只含结构性不变项。
- **轮间漂移报告** `prd2tc compare-runs <runA> <runB>`：
  1. 先比 `prdHash/promptHash/configHash` → 若不等，定性为"非同类运行"（PRD/Prompt/参数变了），不算复现问题；
  2. 同类则比结构指纹 → 输出漂移表：模块增删、每模块 REQ/用例数增减、覆盖率差、门禁差异；零漂移 → `✓ 复现`；
  3. 存在漂移时，附"变化样例"（如新增/删除用例的 id + requirement + title）供人工定位。
- 技术选型参考：结构 diff 用 [deepdiff/jsondiff 等 Python diff 库](https://github.com/smheidrich/comparison-of-generalized-diff-libraries-for-python)；"同一断言在两轮运行间是否成立"的思路参考 [promptfoo 回归断言](https://www.promptfoo.dev/docs/configuration/test-cases/)，但把断言从文本收敛到指纹。
- 门槛化：`compare-runs` 可作为门禁/CI 开关（默认仅报告；可选 `--fail-on-drift` 对 REQ 覆盖缺失、模块增减等硬漂移失败），参考 promptfoo 在 pipeline 中做回归守卫的用法。
- Web（后置）：运行页显示本运行指纹，并支持"与上次同 PRD 运行对拍"；成本低、收益直观。

## 5. 不做 / 明确不承诺

- 不承诺字节级复现（厂商都不承诺，见 §1 引文）。
- 不做语义级 embedding 对比（成本高，收益待验证；列为后续候选）。
- 不改动默认温度与探索型生成的多样性：repro 是"验收模式"，不是唯一模式。

## 6. 交付状态

1. ✅ `Config` 增加 `repro`/`seed`：CLI `--repro`、`--seed`；env `PRD2TC_REPRO`、`PRD2TC_SEED`（服务/CLI 通用）。repro 模式下 `stage_temperature` 恒为 0；OpenAI 兼容端点请求携带 `seed`（Anthropic/MiniMax 忽略）；`run-manifest.parameters` 与 `config_hash` 记录 repro/seed。
2. ✅ `src/prd2tc/reproduce.py`：`run_fingerprint(root)` 生成结构指纹（hash + 模块/REQ/用例数 + 覆盖率 + 门禁）；成功运行后自动落盘 `reproduce.json`。
3. ✅ `prd2tc compare-runs <A> <B> [--out json] [--fail-on-drift]`：源不同单独归因；同源漂移输出明细；退出码 0=复现。
4. ✅ 测试：`tests/test_reproduce.py` ×3（指纹对措辞不敏感 / 同源漂移检出 / 异源归因）；全量 94 passed。
5. 🔲 Web 对拍入口（可选后置，未做）。
6. ✅ 文档：developer-handoff 补充 repro/compare-runs 用法。

