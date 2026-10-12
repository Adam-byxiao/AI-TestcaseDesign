# prd2tc Prompt 与门禁优化方案

> 版本：v1.1 | 编制日期：2026-10-10 | 修订日期：2026-10-10 | 适用范围：`prompts/` 全部资产 + `src/prd2tc/{validator,gates,stages,schema,traceability}.py`
>
> 本文档基于对 prd2tc v0.5.0 的代码审计，以及对 spec2case（90%+ 可用率生产级项目）、CANDOR（ASE 2025）的实现对比分析。

> ## 修订记录（v1.0 → v1.1）
>
> v1.1 对 v1.0 的代码片段做了**实测校验**，修正以下问题（均已在本文档内改正）：
>
> | 编号 | 问题 | 影响 |
> |---|---|---|
> | R-1 | §4.3.2「预期过短」阈值 `_MIN_EXPECTED_LEN = 5` | **误报率 38.9%**（实测 18 条真实样本），且会拦掉 `03-test-cases.md` 自带示例「跳转首页」。**v1.2 起按用户指定从严取 8**（详见 §4.3.1 说明） |
> | R-2 | 空洞断言规则在 §4.3.2 / §4.3.3 / 附录 A 有**三种不一致写法** | 行为不同，无法确定实施口径。已统一为「带长度保护」版本 |
> | R-3 | §4.4.2 `validate_dir` 片段的 `total += len(cases)` **缩进在内层循环** | 用例总数按 `len(cases)` 倍膨胀（实测 3 条 → 9），而它正是 `validate` 的输出 |
> | R-4 | §6.2 `examples_render(examples)` **未定义** | 照抄即 `NameError`，应为 `render_examples` |
> | R-5 | §4.3.3 `VAGUE_EXPECTED_ALLOWLIST` 示意代码**引用了未定义名 `text`** | 该条件表达式语法合法，但模块级执行即 `NameError: name 'text' is not defined`。已删除，改为上下文豁免（长度保护） |
> | R-6 | §8.2 称「6 个失败测试」只需改 `SAMPLE_PRD` 路径 | **不完整**：断言硬编码 `youxuan-mall/v0.3`，必须同步改 |
> | R-7 | §1.2 覆盖率表需随 v0.6.0 更新 | `testData` 校验已落地；覆盖率 35% → 45% |
>
> **基线提示**：本方案行号基于 **v0.5.0**。v0.6.0 改动过 `llm.py` / `stages.py` / `validator.py` / `schema.py` / `service.py` / `io.py`（实时指标、流式、testData 契约、Markdown 中转），
> 行号已漂移 1–5 行（如 `validator.py` 107 → 117 行）。**定位时请以函数名 / 常量名为准，不要依赖行号。**
>
> **与 v0.6.0 已实现能力的关系**（详见 §1.6）：`testData` 嵌套校验、`<think>` 思考分离、Markdown 中转已落地。
> 其中 Markdown 中转（`--markdown-transit`）从**格式层面**消除了 §5.3 要防的转义问题，实施 §5.3 前请先评估该路径。

---

## 目录

- [第一部分：诊断基线](#第一部分诊断基线)
- [第二部分：核心原则](#第二部分核心原则)
- [第三部分：方案一 — 温度与可复现性](#第三部分方案一--温度与可复现性)
- [第四部分：方案二 — 约束下沉为门禁](#第四部分方案二--约束下沉为门禁)
- [第五部分：方案三 — Prompt 资产修订](#第五部分方案三--prompt-资产修订)
- [第六部分：方案四 — 结构重构](#第六部分方案四--结构重构)
- [第七部分：约束漂移检测](#第七部分约束漂移检测)
- [第八部分：实施路线图](#第八部分实施路线图)

---

## 第一部分：诊断基线

### 1.1 现状资产清单

| 资产 | 路径 | 行数 | 版本 | 注入位置 |
|---|---|---|---|---|
| 测试原理层 | `prompts/00-testing-principles.md` | 56 | v0.2.0 | 阶段 1/2/3 任务指令区 |
| 阶段 1 | `prompts/01-prd-analysis.md` | 28 | v0.5.0 | `build_stage_1_user` |
| 阶段 2 | `prompts/02-test-plan.md` | 18 | v0.2.0 | `build_stage_2_user` |
| 阶段 3 | `prompts/03-test-cases.md` | 17 | v0.3.0 | `build_stage_3_user` |
| 用例约束层 | `prompts/04-case-constraints.md` | 16 | v0.3.0 | 阶段 3 输出要求区 |
| 模板 | `templates/test-plan.template.md` | 43 | — | 阶段 2 |
| 门禁 | `src/prd2tc/gates.py` | 66 | — | 三阶段 |
| 校验 | `src/prd2tc/validator.py` | 107 | — | 阶段 3 + CLI validate |

### 1.2 约束覆盖率分析

对 `00-testing-principles.md` 中每条 `[必须]` / `[禁止]` 约束，逐一核对门禁实现：

| # | Prompt 约束 | 出处 | 门禁状态 | 实现位置 |
|---|---|---|---|---|
| 1 | 每条用例标注 `design_method` | 00:26 | ❌ **未实现** | — |
| 2 | 输入有取值域 → 等价类+边界值 | 00:28 | ❌ **未实现** | — |
| 3 | 多条件组合 → 判定表 | 00:29 | ❌ **未实现** | — |
| 4 | 存在状态机 → 状态迁移 | 00:30 | ❌ **未实现** | — |
| 5 | 业务流程 → 场景法 | 00:31 | ❌ **未实现** | — |
| 6 | target 用「元素名 (data-testid)」 | 00:46 | ⚠️ **仅验非空** | `validator.py:92` |
| 7 | 金额格式 ¥ 千分位 2 位小数 | 00:47 | ❌ **未实现** | — |
| 8 | 禁止模糊步骤 | 00:48 | ❌ **未实现** | — |
| 9 | 禁止不可验证预期 | 00:49 | ❌ **未实现** | `validator.py:95` 仅验非空 |
| 10 | 禁止重复用例 | 00:50 | ❠️ **仅人工约定** | `traceability.py:17` 查引用重复，非用例重复 |
| 11 | 禁止用例互依 | 00:50 | ❌ **未实现** | — |
| 12 | P0 必带 `@smoke` | 00:57 | ✅ **已实现** | `validator.py:73` |
| 13 | 全量带 `@regression` | 00:57 | ❌ **未实现** | — |
| 14 | 每 REQ 至少一条用例 | 00:38 | ✅ **已实现** | `traceability.py:53` |
| 15 | id 格式 `TC-<MOD>[-<SUB>]-<SEQ>` | 00:67 | ✅ **已实现** | `validator.py:57` |
| 16 | module ∈ 模块表 | 00:67 | ✅ **已实现** | `validator.py:64` |
| 17 | priority ∈ P0..P3 | 00:67 | ✅ **已实现** | `validator.py:66` |
| 18 | requirement 为 REQ-* 编号数组 | 00:67 | ✅ **已实现** | `validator.py:76`+`traceability.py:50` |
| 19 | steps[].action ∈ 动作枚举 | 00:67 | ✅ **已实现** | `validator.py:90` |
| 20 | testData 扁平化「键=值」 | 04:9-14 | ✅ **已实现**（v0.6.0） | `schema.validate_test_data()` + `validator.py` |

**统计：20 条约束中，门禁完整实现 8 条，部分实现 2 条，完全未实现 10 条。**

> **覆盖率：45%**（部分实现按 0.5 计）
>
> v1.0 记为 35%（7 条完整 + 3 条部分）。v1.1 按 v0.6.0 实现更新：**#20 `testData` 扁平化已下沉为门禁**
> （`schema.validate_test_data()` 拒绝嵌套对象/数组，由 `validator.validate_case()` 调用），
> 因此从「仅在 prompt」升级为「已实现」。其余条目未变。

### 1.3 已确认的约束漂移（BUG 级）

#### 漂移 1：`design_method` 要求存在但示例缺失

- `00-testing-principles.md:26` — `[必须] 每条用例标注 design_method`
- `00-testing-principles.md:67` — 阶段 3 自检项含 `design_method 已标注`
- `04-case-constraints.md:6` — `**design_method**：每条用例标注采用的测试设计方法…**作为可选字段**`
- **但** `03-test-cases.md:18` 的唯一 JSON 示例**不含 `design_method` 字段**

模型面临矛盾选择：

- 遵循 principles → 输出示例中不存在的字段（违反"字段与此一致"的硬要求）
- 遵循示例 → 遗漏 `design_method`（违反 principles 的 `[必须]`）

由于 `03-test-cases.md` 是"输出要求（生成前最后阅读，优先级最高）"，**多数模型选择服从示例**，`design_method` 实际缺失率极高。

> **同一约束在两个 prompt 中一个说"必须"一个说"可选"，且示例不支持。这是可直接修复的输出质量问题。**

#### 漂移 2：`00-testing-principles.md` 声称"注入阶段 1/2/3"，但实际只有 1 和 2

- `00-testing-principles.md:10` — `适用阶段：1 需求分析 / 2 测试方案 / 3 用例生成`
- `stages.py:134` — 阶段 1 通过 `constraints.load_principles()` 注入 ✅
- `stages.py:146` — 阶段 2 通过 `constraints.load_principles()` 注入 ✅
- **但** `stages.py:178-192` `stage_3_system()` 中**没有** `load_principles()`，只有 `constraints.build_case_constraints(examples)`（即 04 模板）

阶段 3 实际只收到 `04-case-constraints.md`，**未收到 `00-testing-principles.md`**。

> 这解释了为什么 00 中"每条用例标注 design_method""禁止不可验证预期"等约束对阶段 3 完全无效——它们根本没进入阶段 3 的 prompt。

### 1.4 温度基线

`stages.py:53`：

```python
_DEFAULT_TEMP = {1: 0.2, 2: 0.3, 3: 0.5}
```

对比 spec2case 各环节：

| 环节 | spec2case | prd2tc |
|---|---|---|
| 需求审查 | 0.2 | 0.2 ✅ |
| PRD 分块 | 0.1 | — |
| 知识组织 | 0.1 | — |
| 用例评审 | 0.1 | — |
| 用例生成 | 0.3 | **0.5** ⚠️ |

阶段 3 是全链路最需要确定性的环节（结构化 JSON、字段强校验、程序化重编号），却是**温度最高的一环**。

### 1.5 可复现性来源分析

| 机制 | 位置 | 复现贡献 | 备注 |
|---|---|---|---|
| 程序化重编号 | `stages.py:523-527` `renumber()` | **最强** | id 完全由程序生成，模型不可控 |
| 模块代号锚定 | `stages.py:391-399` | 强 | 从 PRD 编号前缀反推模块码 |
| 需求指纹去重 | `stages.py:405` | 强 | `diff.fingerprint()` |
| 顺序化编号 | `stages.py:424-443` | 强 | 按模块内出现顺序 |
| 字段枚举 | `schema.py:6-11` | 中 | 13 个 action 硬枚举 |
| **自然语言约束** | `prompts/*.md` | **弱/负** | 模糊约束引入方差 |

> **关键结论：可复现度主要来自"程序化生成"和"枚举约束"，而非"prompt 约束的数量与详细度"。**
>
> 模糊的自然语言约束（"要写得专业""尽量覆盖全面"）每轮给出不同结果，是方差的**来源**而非抑制手段。

### 1.6 与 v0.6.0 已实现能力的关系（v1.1 新增）

本方案基于 v0.5.0 编写，而 v0.6.0 已落地三项与本方案**部分重叠**的能力。实施前请先确认差异，避免重复劳动：

| v0.6.0 能力 | 位置 | 与本方案的关系 |
|---|---|---|
| **testData 扁平化契约 + 校验** | `schema.validate_test_data()` / `validator.py` | **已完成 §1.2 #20**。契约收紧为「键=值」字符串数组，拒绝嵌套对象/数组（嵌套需多层转义，实测导致模型整轮输出归零） |
| **`<think>` 思考分离** | `reasoning.py` | **部分完成 §5.3 的目标**。推理内容单独统计、提取时优先剥离；实测 M3 会把思考写进 `content`，曾出现 26,504 字符输出 100% 是思考、正文为 0 |
| **Markdown 中转**（`--markdown-transit`） | `markdown_transit.py` | **从格式层面替代 §5.3**。模型输出 Markdown 表格再由程序转 JSON：表格无引号嵌套，转义类失败从根上消失；实测字符 −50%、双引号 728 → 0，落盘产物仍是 JSON |
| 预算耗尽自愈（批次对半拆分） | `stages._split_batch_if_budget_exhausted()` | 与 §4 门禁互补：门禁负责**发现**，自愈负责**重试** |

**因此实施 §5.3（JSON 转义约束）前必须先回答一个问题**：转义问题要用「prompt 约束」还是「格式规避」解决？

- 若已启用 `--markdown-transit`，§5.3 的收益大幅下降（表格里不存在引号嵌套）；
- 若坚持 JSON 输出，§5.3 仍然必要，且**应优先于** §4.6 金额校验等次要门禁。

> 建议：把 §5.3 标注为「依赖输出格式决策」，在 §8 路线图中排在 `--markdown-transit` 评估之后。

---

## 第二部分：核心原则

### 2.1 三条设计原则

**原则一：约束的价值在于可判定性，不在于篇幅**

> 一个能被程序验证的短约束，胜过一段无法验证的长描述。

判断标准：能否写出对应的 `assert` 语句？能 → 高价值；不能 → 低价值。

**原则二：给模型划定"什么不算问题"比增加禁令更有效**

spec2case 在 writer prompt 中的做法：

```markdown
# 普通 LU 重复控制补充
- 普通 LU 可以保留对象不同、边界不同、断言不同的相似用例。
- 同一功能点的多种输入、多个状态、多个边界值得单点验证可以保留。

# 链路 LU 重复控制补充
- 链路 LU 只保留能体现链路变化的用例，不要把同一链路拆成多个只差一个单点步骤的用例。
```

对比 prd2tc `00-testing-principles.md:50`：

```markdown
[禁止] 重复用例（同步骤 + 同预期）、用例互赖、为凑数量的同质用例。
```

后者更严但"同质"未定义，模型只能猜测 → 每轮判定不同 → 方差。

**原则三：示例 > 描述**

模型对示例的服从度远高于自然语言描述。当 prompt 要求与示例冲突时，**示例胜出**。

> 漂移 1 就是这条原则的直接后果：示例缺 `design_method`，导致该字段全链路缺失。

### 2.2 不做什么

明确排除以下方向，避免过度工程化：

| 不做 | 原因 |
|---|---|
| ❌ 把门禁交给 LLM 判断 | 破坏"机器强制"核心卖点，且引入不可复现性 |
| ❌ 引入 CANDOR 式多模型共识 | 论文自陈单文件耗时数小时，不可用于 CI |
| ❌ 引入向量库 RAG 进主链路 | spec2case 作者明确反对："知识库本身质量不好反而适得其反" |
| ❌ 放宽 schema 枚举 | prd2tc 产物供 Playwright 消费，机器可读性优先于灵活性 |
| ❌ 大规模重写 prompt 结构 | 收益不确定，风险高；应做可验证的增量改动 |

---

## 第三部分：方案一 — 温度与可复现性

### 3.1 改动内容

将阶段 3 默认温度从 `0.5` 调整为 `0.3`。

**文件**：`src/prd2tc/stages.py`

```python
# 修改前（line 53）
_DEFAULT_TEMP = {1: 0.2, 2: 0.3, 3: 0.5}

# 修改后
_DEFAULT_TEMP = {1: 0.2, 2: 0.3, 3: 0.3}
```

### 3.2 生效方式（零代码验证）

修改前可通过环境变量立即验证效果：

```bash
export PRD2TC_TEMP_S3=0.3
prd2tc generate --prd examples/sample-prd.md --stub  # stub 不受影响
prd2tc generate --prd <真实PRD> --examples examples/ --out output
```

`stages.py:63` 的优先级链保持不变：

```python
raw = os.environ.get(f"PRD2TC_TEMP_S{stage}") or os.environ.get("PRD2TC_TEMPERATURE")
return float(raw) if raw else _DEFAULT_TEMP[stage]
```

`repro=True` 时恒为 0（`stages.py:61`），不受影响。

### 3.3 风险与回退

| 风险 | 概率 | 回退 |
|---|---|---|
| 低温度导致用例多样性下降 | 中 | 可对模块级首次尝试用 0.4，重试修正用 0.2 |
| 边界值覆盖不足 | 低 | 在 `04-case-constraints.md` 中显式列出边界值要求 |

**A/B 验证方法**：固定 PRD 跑 `--repro` 模式 3 次，`compare-runs` 对拍结构指纹（`cli.py:203`）。温度 0.3 下若结构指纹一致率显著高于 0.5，则改默认。

### 3.4 与可复现性的关系

`--repro` 模式（`stages.py:61`）已将温度归零，用于回归验收基线。默认温度降到 0.3 后，非 repro 模式的行为更接近 repro 基线，**日常运行与回归验收的差异缩小**，有利于建立稳定的期望值。

---

## 第四部分：方案二 — 约束下沉为门禁

### 4.1 概述

把 `00-testing-principles.md` 中已写明但门禁未实现的约束，**下沉为 `validator.py` 的校验规则**。

**核心收益**：

- 模型收到**具体错误信息**（"用例 X 的 design_method 缺失，prompt 明确要求"）而非模糊指引，修正成功率高得多
- 纯代码实现，零模型调用成本
- 只收窄不放松输出空间，**不降低可复现性**

### 4.2 P0-1：design_method 强制

#### 4.2.1 前置修复（必须先做）

**文件**：`prompts/03-test-cases.md`

修改 line 18 的 JSON 示例，在 `priority` 后插入 `design_method`：

```json
{"id":"TC-AUTH-LOGIN-001","module":"AUTH","subModule":"LOGIN",
"title":"正确凭据登录成功","priority":"P0",
"design_method":"等价类划分",
"tags":["@smoke","@regression","@auth"],
"requirement":["REQ-AUTH-002"],
"preconditions":["存在 normal 状态账号"],
"testData":["phone=13800138000","password=123456"],
"steps":[{"action":"goto","target":"/login","data":""},
         {"action":"fill","target":"手机号输入框","data":"13800138000"},
         {"action":"click","target":"登录按钮","data":""},
         {"action":"expectUrl","target":"/","data":""}],
"expected":["跳转首页"]}
```

同步修改 `prompts/04-case-constraints.md:6`，移除"作为可选字段"：

```markdown
1. **design_method**：每条用例**必须**标注采用的测试设计方法
   （等价类划分 / 边界值分析 / 判定表 / 状态迁移 / 场景法 / 错误推测）。
```

#### 4.2.2 schema 扩展

**文件**：`src/prd2tc/schema.py`

```python
# 在 SCHEMA_VERSION 旁新增设计方法枚举，保持与 prompt 单一事实源
DESIGN_METHODS = ("等价类划分", "边界值分析", "判定表", "状态迁移", "场景法", "错误推测")
```

> 不修改 `SCHEMA_VERSION`（`schema.py:4` 的 `2.0`）——新增可选字段属向后兼容；`normalize_cases()`（`schema.py:14`）无需改动。

#### 4.2.3 validator 实现

**文件**：`src/prd2tc/validator.py`

新增导入：

```python
from prd2tc.schema import (ACTION_SCHEMA_VERSION, ACTIONS_BY_VERSION, DESIGN_METHODS)
```

在 `validate_case()` 中，`priority` 校验之后插入：

```python
    # design_method：00-testing-principles.md:26 [必须]
    method = case.get("design_method")
    if not method:
        errors.append(f"{at} 缺少 design_method（必须标注测试设计方法）")
    elif str(method) not in DESIGN_METHODS:
        errors.append(
            f"{at} design_method 非法: {method}；"
            f"可选值 {sorted(DESIGN_METHODS)}"
        )
```

#### 4.2.4 下游影响

- `stages.py:620` 的 `renumber()` 不受影响（只改 id 和 module）
- `io.py` 的 `save_cases()` 直接序列化，无需改
- **兼容性**：旧版用例 JSON 无 `design_method` 字段，`prd2tc validate` 会报错。这是**预期的**——门禁收紧即行为变更，需在 CHANGELOG 说明。若需过渡期，可用 `validate_case(..., strict_design_method=False)` 参数开关（默认 True）。

### 4.3 P0-2：不可验证预期检测

对应 `00-testing-principles.md:49`：`[禁止] 不可验证预期（"表现正常""无异常"）`

#### 4.3.1 词表设计

**文件**：`src/prd2tc/validator.py`

```python
# 不可验证的空洞预期：常出现在「表现正常」「无异常」这类无断言价值的表述中
VAGUE_EXPECTED_TOKENS = (
    "表现正常", "显示正常", "功能正常", "运行正常", "工作正常",
    "无异常", "没有异常", "正常显示", "符合预期", "正确显示",
    "显示正确", "功能可用", "无报错", "不报错",
)

# 过短的预期：len(text) <= 该值 且不含数字 → 判过短
# v1.2 起按用户指定**从严取 8**（v1.1 曾用 3）。
# 取 8 的含义：预期须写成 >8 字的完整句子，把「页面渲染」「合计归零」这类
# 4~6 字短语挡在门外；含具体数字的断言仍豁免长度。
# ⚠️ 代价（实测 18 条真实样本）：拦截率 38.9%，含示例库 2 条。
#    因此示例库 / prompt 示例 / 文档**必须同步改写**，否则模型照抄示例即被拦。
_MIN_EXPECTED_LEN = 8
```

#### 4.3.2 校验实现

> **v1.1 修订（R-2）**：v1.0 在此处给出「无长度保护」写法，与 §4.3.3、附录 A 的「带长度保护」写法行为不同，
> 且与本段下方「设计说明」自相矛盾。现统一为**带长度保护**版本（与附录 A 一致），并修正阈值（见 4.3.1）。
>
> 空洞词表命中后**仍需长度保护**：`「页面跳转正常，提示"余额不足"」` 含具体内容，不应判错。

在 `validate_case()` 的 `expected` 校验段替换：

```python
    expected = case.get("expected")
    if not isinstance(expected, list) or len(expected) == 0:
        errors.append(f"{at} expected 为空")
    else:
        for item in expected:
            text = str(item).strip()
            if not text:
                errors.append(f"{at} expected 含空字符串")
                continue
            # 空洞预期检测（00-testing-principles.md:49）
            # 长度保护：长句通常含具体内容，短句才可能是纯空洞表述
            if len(text) <= 12 and any(token in text for token in VAGUE_EXPECTED_TOKENS):
                errors.append(
                    f"{at} 预期不可验证（空洞断言）: {text!r}；"
                    f"请改为可断言的表述（具体 UI 元素/数据/状态/金额）"
                )
            elif len(text) <= _MIN_EXPECTED_LEN and not any(ch.isdigit() for ch in text):
                errors.append(
                    f"{at} 预期过短且无具体值: {text!r}；"
                    f"请补充具体断言对象"
                )
```

> **设计说明**：
> - 词表匹配 + **长度保护**（≤12）——避免误伤「页面跳转正常，提示"余额不足"」这类含具体内容的合法表述
> - 长度阈值 `_MIN_EXPECTED_LEN = 8`（**用户指定从严**；v1.1 曾用 3，v1.0 原为 5）。
>   含义：预期须 **>8 字**，把「页面渲染」「合计归零」「跳转首页」这类短语挡在门外；
>   含数字的断言豁免长度。
> - **附带成本（必须知晓）**：实测 18 条真实样本拦截率 **38.9%**，其中含示例库 2 条
>   （`不进入登录态`、`合计归零`）与真实产物 5 条（`页面渲染`）。
>   因此落地时**同步改写**为：`仍停留在登录页且不进入登录态`、`删除后小计与合计金额均归零`、
>   `页面成功渲染并显示预期内容`。若不同步改写，模型照抄示例即被拦，
>   会重现 R-1 的「示例→门禁→重试」死循环并显著增加重试成本。
> - 这两条规则的目的是**引导模型写出具体断言**，错误信息必须包含"请改为…"的明确指引，否则模型不知道往哪改
>
> **必配回归测试**：见 §8.2 第二批——「门禁不得拦掉 `03-test-cases.md` 自带示例」。

#### 4.3.3 反向影响检查

**风险**：词表可能误伤合法用例。**缓解**：长度保护 + 先在现有样例产物上跑一遍统计误报率。

> **v1.1 修订（R-5）**：v1.0 此处的 `VAGUE_EXPECTED_ALLOWLIST` 示意代码**引用了未定义名 `text`**
> （`("无异常" if "无异常提示" in text else "",)`）——它的条件表达式**语法合法**，但作为模块级语句执行时
> 立即 `NameError: name 'text' is not defined`，因此无法按原样使用。已删除，改为下面的纯函数实现。

长度保护本身即「上下文豁免」：短句才判空洞，长句放行。统一实现在 `validator.py`，
**不要在别处重复定义**（这是 R-2 的根因）：

```python
def _is_vague(text: str) -> bool:
    """空洞断言检测：词表命中 + 长度保护。

    长度保护的意义：长句通常含具体内容（如「页面跳转正常，提示"余额不足"」），
    不应因出现「正常」二字就判错。
    """
    if len(text) > 12:
        return False
    return any(token in text for token in VAGUE_EXPECTED_TOKENS)
```

**误报率验证（实测数据，供实施时对照）**：

| 规则 | 样本 | 拦截 | 拦截率 |
|---|---|---|---|
| 词表命中（带 ≤12 长度保护） | 18 | 0 | **0.0%** |
| 长度 ≤3 且无数字（v1.1 取值，已弃用） | 18 | 0 | 0.0% |
| 长度 ≤5 且无数字（v1.0 原阈值） | 18 | 6 | 33.3% |
| **长度 ≤8 且无数字（v1.2 现行，用户指定）** | 18 | **7** | **38.9%** |
| 长度 ≤10 且无数字（不建议） | 18 | 11 | 61.1% ← 会拦掉 prompt 自带示例「登录成功后跳转首页」(9) |

样本 = `03-test-cases.md` 示例 + `examples/youxuan-mall-examples.json` + 真实落盘产物。

> **阈值 8 的取舍**：它显著提高了对「完整可断言句子」的要求，代价是历史样本拦截率 38.9%。
> 已同步改写示例库与 prompt 示例以维持「示例必须过门禁」这一硬约束。
> 若观察到真实运行的重试率过高，回退到 3 只需改一处常量
> （但**必须**同时更新 04 文档与示例库，`tests/test_gate_constraints.py` 会强制提醒）。

### 4.4 P0-3：重复用例检测

对应 `00-testing-principles.md:50`：`[禁止] 重复用例（同步骤 + 同预期）`

#### 4.4.1 签名设计

**文件**：`src/prd2tc/validator.py`

```python
import hashlib

def _case_signature(case: dict) -> str:
    """用例去重签名：模块 + 步骤序列 + 预期序列。

    归一化规则：去除空白、统一小写、剔除数据值差异（同一操作序列视为重复，
    即使 testData 不同——这正是「同步骤+同预期」的 prompt 定义）。
    """
    def norm(text) -> str:
        return re.sub(r"\s+", "", str(text or "")).lower()

    steps = "|".join(
        f"{norm(s.get('action'))}>{norm(s.get('target'))}"
        for s in (case.get("steps") or []) if isinstance(s, dict)
    )
    expected = "|".join(norm(e) for e in (case.get("expected") or []))
    module = norm(case.get("module"))
    raw = f"{module}#{steps}#{expected}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
```

#### 4.4.2 批量校验

在 `validate_dir()` 中增加跨文件去重——**同模块用例可能分布在不同 JSON 文件**：

> **v1.1 修订（R-3）**：v1.0 片段把 `total += len(cases)` 缩进在内层循环里，
> 会让用例总数按 `len(cases)` 倍膨胀（实测 3 条用例得到 `total = 9`）。
> 而 `total` 正是 `prd2tc validate` 的输出（"校验通过：N 条用例"），也是 `stats` 的分母。
> 同时该片段的 f-string 引用了未定义的 `at`。下面为修正版。

```python
def validate_dir(cases_dir, allowed_modules=None, *, check_duplicates=True):
    ...
    signatures: dict[str, str] = {}   # signature -> 首个用例 id
    for f in sorted(d.glob("*.json")):
        ...
        for i, case in enumerate(cases):
            at = f"[文件 {f.name} 用例#{i + 1} {case.get('id', '(无id)')}]"
            errors.extend(validate_case(case, total + i, seen, allowed_modules))
            if check_duplicates:
                sig = case_signature(case)
                if sig in signatures:
                    errors.append(
                        f"{at} 与用例 {signatures[sig]} 重复"
                        f"（同模块+同步骤+同预期）"
                    )
                else:
                    signatures[sig] = str(case.get("id", ""))
        total += len(cases)        # ← 必须在外层文件循环内、内层用例循环外
    return total, errors
```

> **回归测试要求**：`validate_dir` 的 `total` 必须等于各文件用例数之和。
> 这是 R-3 的直接防护——统计口径错了不会报错，只会静默给出错误的数字。

#### 4.4.3 阶段 3 门禁同步

`gates.check_cases_text()`（`gates.py:68`）在单次 LLM 输出内也需检测重复：

```python
def check_cases_text(text, allowed_modules=None):
    ...
    seen: set[str] = set()
    errors = [e for i, c in enumerate(cases) for e in validate_case(c, i, seen, allowed_modules)]
    # 新增：批内重复检测
    signatures: dict[str, str] = {}
    for c in cases:
        sig = _case_signature(c)
        if sig in signatures:
            errors.append(
                f"[用例 {c.get('id', '(无id)')}] 与 {signatures[sig]} 重复"
                f"（同模块+同步骤+同预期），请删除其一或改用不同的步骤/预期"
            )
        else:
            signatures[sig] = str(c.get("id", ""))
    return errors
```

需在 `gates.py` 导入 `case_signature`（**公开名**，不要跨模块引用 `_case_signature` 这类私有名）。

### 4.5 P1：覆盖维度统计

对应 `00-testing-principles.md` 第 3 节（正向/反向/边界/异常/状态）。

#### 4.5.1 设计

**不做门禁拦截，改为统计报告**。理由：维度归类本身有歧义，强门禁误报率高。

**文件**：`src/prd2tc/traceability.py`

```python
# 覆盖维度识别的启发式关键词（零依赖，可从示例库学习后扩充）
DIMENSION_HINTS = {
    "正向":   ("正常", "成功", "正确", "有效", "happy"),
    "反向":   ("失败", "错误", "非法", "无效", "异常提示", "拒绝"),
    "边界":   ("边界", "临界", "上限", "下限", "最大", "最小", "刚好", "等于"),
    "异常":   ("超时", "中断", "断网", "空数据", "空态", "未支付", "过期"),
    "状态":   ("状态", "禁用", "启用", "冻结", "解冻", "流转"),
}


def _infer_dimension(case: dict) -> set[str]:
    """启发式推断用例覆盖的维度（可多标签）。"""
    text = " ".join([
        str(case.get("title", "")),
        str(case.get("design_method", "")),
        *map(str, case.get("tags") or []),
        *[str(s.get("data", "")) for s in (case.get("steps") or []) if isinstance(s, dict)],
        *[str(e) for e in (case.get("expected") or [])],
    ]).lower()
    return {dim for dim, kws in DIMENSION_HINTS.items() if any(k in text for k in kws)}


def build_dimension_coverage(cases: list[dict]) -> dict:
    """按模块统计覆盖维度，识别维度缺口。"""
    by_module: dict[str, set[str]] = defaultdict(set)
    for case in cases:
        if case.get("status") in {"deprecated", "superseded"}:
            continue
        by_module.setdefault(str(case.get("module", "")), set()).update(_infer_dimension(case))
    expected = {"正向", "反向", "边界"}
    gaps = {
        module: sorted(expected - dims)
        for module, dims in by_module.items()
        if expected - dims
    }
    return {
        "schemaVersion": "1.0",
        "byModule": {m: sorted(d) for m, d in sorted(by_module.items())},
        "gaps": gaps,
        "summary": {
            "modulesWithGaps": len(gaps),
            "totalModules": len(by_module),
        },
    }
```

#### 4.5.2 集成点

在 `pipeline.py:243` 后追加：

```python
matrix = build_traceability(reqs, cases, min_cases=min_cases) if not errors else {}
dimensions = build_dimension_coverage(cases)
# 写入 traceability.json（与 matrix 同级）
```

**UI 呈现**：`RunPage.tsx` 的模块表格增加"维度"列，缺口模块标 🟡 提示，**不阻断**。

### 4.6 P2：金额格式校验

对应 `00-testing-principles.md:47` 与 `04-case-constraints.md:8`。

```python
# 金额断言格式：¥ + 千分位 + 2 位小数，如 ¥4,727.05
AMOUNT_PATTERN = re.compile(r"¥\s?\d{1,3}(,\d{3})*\.\d{2}")
LOOSE_AMOUNT = re.compile(r"\d+\s*元|\d+\.\d+\s*元")

# 在 expected 校验中追加
for item in expected:
    text = str(item)
    if LOOSE_AMOUNT.search(text):   # 出现「1234.5元」这类
        errors.append(
            f"{at} 金额格式不规范: {text!r}；"
            f"必须使用 ¥ 千分位 2 位小数（如 ¥4,727.05）"
        )
```

> **注意**：仅当 expected 中确实出现金额时才校验，不要强制每条用例都有金额断言。用 `LOOSE_AMOUNT` 而非"含数字"作为触发条件，避免误伤。

---

## 第五部分：方案三 — Prompt 资产修订

### 5.1 修复 principles 注入阶段 3

**问题**：`00-testing-principles.md` 声称注入 1/2/3，实际只有 1/2。

**方案**：二选一。

**方案 A（推荐）**：修正 principles 的声明，把阶段 3 的约束收敛到 `04-case-constraints.md`（避免 token 膨胀）。

修改 `00-testing-principles.md:10`：

```markdown
> 适用阶段：1 需求分析 / 2 测试方案（阶段 3 的用例级约束见 `04-case-constraints.md`）。
```

**方案 B**：在 `stage_3_system()` 中注入 principles。

`stages.py:178-192` 修改：

```python
def stage_3_system(plan_md: str, examples: list[Example] | None = None) -> str:
    parts = [
        SYSTEM_ROLES[3],
        "以下是本次任务各模块共享、保持不变的上下文（测试方案 / 字段约束 / 用例生成约束 / 参考示例）：",
        "## 测试方案\n" + plan_md,
        "## 测试原理与硬约束\n" + constraints.load_principles(),
        "## 字段约束\n" + field_spec(),
        "## 用例生成约束\n" + constraints.build_case_constraints(examples),
    ]
    ...
```

> **成本评估**：principles 约 1800 tokens，加入稳定 system 前缀后**可被 prompt cache 覆盖**（前提是 `PRD2TC_PROMPT_CACHE=1` 且 Provider 支持）。`llm.py:270-271` 的 Anthropic 分支已支持 `cache_control`。
>
> **推荐方案 A**：token 更省，且阶段 3 需要的约束已在 04 中。用方案 B 需先验证缓存命中。

### 5.2 消除重复控制的歧义

**文件**：`prompts/00-testing-principles.md:50`

修改前：

```markdown
- [禁止] 重复用例（同步骤 + 同预期）、用例互赖、为凑数量的同质用例。
```

修改后：

```markdown
- [必须] **相似不等于重复**：不同对象、不同边界值、不同断言的用例应保留；
  仅当「模块 + 步骤序列 + 预期序列」三者完全一致时才判定为重复。
- [禁止] 用例互相依赖（用例 B 需要用例 A 先执行）；[必须] 每条用例可独立执行。
- [禁止] 为凑数量生成的同质用例（title 不同但步骤与预期实质相同）。
```

**设计说明**：原规则"重复用例（同步骤 + 同预期）"未定义"同质"，导致模型每轮判定不同。新规则给出**可判定的三要素**，与 `_case_signature()`（4.4.1）的实现口径完全一致——prompt 说的和门禁验的是同一件事。

### 5.3 补充 JSON 输出的字符级约束

**文件**：`prompts/03-test-cases.md` 输出要求区

参考 spec2case `test_case_writer_common.py:71-72` 的实战经验，补充：

```markdown
- **JSON 转义（高频失败点）**：
  - 字符串内禁止出现未转义的反斜杠 `\`；路径统一用正斜杠 `/`；
  - 字符串内禁止出现双引号 `"`，需要引号时用中文「」或『』；
  - 字符串内禁止真实换行，多值用「；」分隔；
  - 数组元素之间用逗号分隔，**数组末尾不留逗号**；
  - 输出前逐条检查：是否存在裸反斜杠 / 裸引号 / 尾随逗号。
```

### 5.4 target 格式约束的可执行化

`00-testing-principles.md:46` 要求 `target` 用「页面元素中文名 (data-testid)」格式，但：

- PRD 中未必有 data-testid（这是前端实现细节，PRD 通常不写）
- 阶段 3 有 `reload` / `waitFor` 这类无定位目标的动作（`stages.py:213` 已处理）

**建议**：将约束改为**分级要求**：

```markdown
- [必须] **target 可定位**：优先使用「页面元素中文名 (data-testid)」；
  PRD 未提供 data-testid 时使用「页面.元素中文名」（如「订单页.提交按钮」）。
- [必须] 无定位目标的动作（reload / waitFor）target 填「当前页面」/「页面就绪」。
```

**门禁实现**（对应 4.x 系列，`validator.py`）：

```python
    # target 格式：既不能是纯路径（goto 除外），也不能是空洞描述
    for s in steps:
        action = s.get("action")
        target = str(s.get("target") or "").strip()
        if action in DEFAULT_STEP_TARGETS:
            continue  # reload/waitFor 允许默认值
        if action != "goto" and (not target or target in {"元素", "按钮", "输入框", "页面"}):
            errors.append(
                f"{at} step.target 过于笼统: {target!r}；"
                f"请使用「页面.元素名」或「元素名 (data-testid)」"
            )
```

> **不建议**强制要求 `(data-testid)` 格式——这会让模型在 PRD 无该信息时产生幻觉。分级要求更符合实际。

### 5.5 阶段 3 处理步骤对齐实际输入

**文件**：`prompts/03-test-cases.md:10`

修改前：

```markdown
1. 对照测试方案与 PRD 需求点，建立 REQ → 用例映射（保证全覆盖）。
```

问题：阶段 3 的 user 消息中**只有本批次的结构化需求**（`stages.py:166-168`），没有完整 PRD 全文。"对照 PRD 需求点"指向了不存在的输入。

修改后：

```markdown
1. 对照「结构化需求」中本批 REQ 列表，逐条建立 REQ → 用例映射（保证本批全覆盖）。
   「模块原文上下文」仅用于理解 PRD 原文表述，不得引入其中未在结构化需求中出现的需求点。
```

> 这与 `04-case-constraints.md` 的原则一致（"requirement 只能引用本模块真实存在的 REQ"），但提前在处理步骤里明确，避免模型试图从原文上下文"发现"新需求。

### 5.6 阶段 2 门禁的实际缺口

`gates.check_plan()`（`gates.py:58`）只检查 8 个章节标题是否存在。以下 prompt 要求**未实现**：

| `02-test-plan.md` 要求 | 门禁状态 |
|---|---|
| In Scope / Out Scope 明确区分（line 10） | ❌ |
| 每层策略能落到具体用例类别（line 19） | ❌ |
| BR-* 金额/状态类规则给出边界值建议（line 20） | ❌ |
| 准入/准出**可度量**（line 22） | ❌ |

**建议新增**（`gates.py`）：

```python
def check_plan(text: str) -> list[str]:
    """阶段 2 门禁：必备章节 + 可落地性。"""
    errors = _missing_sections(text, PLAN_SECTIONS)
    # Out Scope 必须显式存在（防止"范围"章节只写 In Scope）
    if "Out Scope" not in text and "不在范围" not in text and "范围外" not in text:
        errors.append("缺少 Out Scope（明确排除项及原因），禁止只写 In Scope")
    # 可度量性：准入准出必须含可观测指标
    criteria_text = _extract_section(text, ["准入", "准出"])
    metrics = ("覆盖率", "通过率", "缺陷", "阻断", "%", "指标", "阈值", "无严重")
    if criteria_text and not any(m in criteria_text for m in metrics):
        errors.append("准入/准出标准不可度量：必须包含具体指标或阈值（如覆盖率、缺陷数）")
    return errors
```

---

## 第六部分：方案四 — 结构重构

### 6.1 动机

`stages.py:178-192` 的 `stage_3_system()` 用 if/else 拼装 system prompt：

```python
def stage_3_system(plan_md, examples=None):
    parts = [
        SYSTEM_ROLES[3],
        "...",
        "## 测试方案\n" + plan_md,
        "## 字段约束\n" + field_spec(),
        "## 用例生成约束\n" + constraints.build_case_constraints(examples),
    ]
    if examples:
        parts.append("## 参考示例\n" + render_examples(examples))
    return "\n\n".join(parts)
```

当前只有单一类型，逻辑清晰。**但若引入链路用例层（见 6.3），会出现 normal/integration 两套约束的组合分支**，届时 if/else 会迅速失控。

### 6.2 模板片段化设计

参考 spec2case `test_case_writer_common.py` 的做法，新建 `src/prd2tc/prompt_parts.py`：

```python
"""阶段 3 prompt 片段：按生成目标组合，供 system 前缀与增量修复复用。"""

COMMON_FACT_RULES = """# 通用事实规则
- 「结构化需求」是本次生成的唯一需求事实来源。
- 「模块原文上下文」仅用于理解 PRD 原文表述，不得引入未在结构化需求中出现的需求点。
- requirement 只能引用本批真实存在的 REQ-*，不得臆造。
"""

NORMAL_COVERAGE_RULES = """# 普通模块写作侧重点
- 以本批 REQ 的完整覆盖为目标，优先写透单个功能的正向、反向、边界与异常。
- 相同功能点的不同输入、不同状态、不同边界应拆为独立用例（对象/边界/断言不同即非重复）。
- 优先级偏向单点核心功能被阻断、规则失效、数据错误。
"""

INTEGRATION_COVERAGE_RULES = """# 链路用例写作侧重点
- 以 flow_hint 与 flow_outline 为中心，覆盖流程穿越、状态传递、数据一致性与异常收尾。
- 只保留能体现链路变化的用例，不重复单点功能的原子规则（字段展示、按钮可见性等）。
- 链路用例必须跨越至少 2 个模块，requirement 需同时引用多个模块的 REQ。
- 优先级偏向链路中断、状态传递错误、数据不一致、闭环失败。
"""

COMMON_OUTPUT_RULES = """# 输出格式
- 只输出单个 JSON 数组（可用 ```json 围栏包裹），不要输出 JSON 之外的任何字符。
- 每个字符串值内禁止出现双引号、反斜杠或真实换行；需要引号用「」，多值用「；」。
- 数组元素间用逗号分隔，末尾不留逗号。
"""


def build_stage3_parts(*, plan_md: str, examples=None, mode: str = "normal",
                       markdown_transit: bool = False) -> str:
    """按生成目标组合 system 前缀片段。

    mode: "normal"（模块用例）| "integration"（链路用例）
    markdown_transit: 透传 v0.6.0 的 Markdown 中转（表格输出，规避 JSON 转义）
    片段顺序即最终拼接顺序——保持字节稳定以命中 prompt cache。
    """
    from prd2tc import constraints, stages  # 延迟导入避免循环
    from prd2tc.examples import render_examples   # ← v1.1 修正 R-4：原名 examples_render 未定义
    parts = [
        stages.SYSTEM_ROLES[3],
        COMMON_FACT_RULES,
        NORMAL_COVERAGE_RULES if mode == "normal" else INTEGRATION_COVERAGE_RULES,
        "## 测试方案\n" + plan_md,
        stages.field_spec(markdown_transit=markdown_transit),
        constraints.build_case_constraints(examples),
        COMMON_OUTPUT_RULES,
    ]
    if examples:
        parts.append("## 参考示例\n" + render_examples(examples))
    return "\n\n".join(parts)
```

> **v1.1 修订（R-4）**：v1.0 此处写作 `examples_render(examples)`，该函数**不存在**（实际名为 `render_examples`，
> 见 `src/prd2tc/examples.py`），照抄会 `NameError`。
>
> **v1.1 修订（R-8，与 v0.6.0 冲突）**：v0.6.0 已给 `stage_3_system()` 增加 `markdown_transit` 关键字参数
> （默认 `False`），用于切换 JSON / Markdown 表格输出。v1.0 提议的
> `stage_3_system(plan_md, examples=None, mode='normal')` **会丢掉该参数**，导致中转模式失效。
> 上面的 `build_stage3_parts` 已保留并透传它；同理 `field_spec()` 需接受 `markdown_transit`
> （v0.6.0 由 `_table_instruction()` 实现该分支）。

**要点**：

1. **片段顺序固定** → system 前缀字节稳定 → prompt cache 可命中
2. **normal/integration 差异集中在单个片段** → 新增链路类型不影响其他逻辑
3. `stages.py` 的 `stage_3_system()` 改为薄封装：

```python
def stage_3_system(plan_md, examples=None, mode: str = "normal",
                   *, markdown_transit: bool = False) -> str:
    from prd2tc.prompt_parts import build_stage3_parts
    return build_stage3_parts(plan_md=plan_md, examples=examples, mode=mode,
                              markdown_transit=markdown_transit)
```

> **循环依赖**：`prompt_parts` 需引用 `stages.SYSTEM_ROLES` 与 `stages.field_spec`，而 `stages` 又需引用 `prompt_parts`。用**函数内延迟导入**（在 `build_stage3_parts` 内部 `from prd2tc import stages`）打破环。

### 6.3 链路用例层（可选，收益大改动大）

#### 6.3.1 现状缺口

`stages.py:509-511` 仅按模块分组：

```python
grouped: dict[str, list[dict]] = defaultdict(list)
for req in requirements:
    if req.get("module"):
        grouped[str(req["module"])].append(req)
```

**缺失**：跨模块业务链路的用例。spec2case 用 `integration_lu` 专门覆盖这一类，并强制"每个链路 LU 至少覆盖 2 个 normal LU"。

#### 6.3.2 基础设施已就绪

`traceability.py` 已有 `caseToRequirements` 双向映射，且 `validate_case` 已支持跨模块 requirement 数组（`validator.py:76-81` 只校验格式，不校验模块归属——但 `stages.py:612-617` 的 `wrong_refs` 检查在模块路径下会拦截跨模块引用）。

**注意**：若要支持链路用例，需**放宽** `stages.py:612-617` 的模块内引用限制：

```python
# 当前（stages.py:612-617）：模块批次内禁止引用他模块 REQ
wrong_refs = [(c.get("id"), ref) for c in cases for ref in c.get("requirement", [])
              if str(ref) not in expected_req_ids]

# 调整后：允许引用本次链路覆盖范围内的所有 REQ
wrong_refs = [(c.get("id"), ref) for c in cases for ref in c.get("requirement", [])
              if str(ref) not in allowed_req_ids]   # allowed_req_ids = 链路范围内的 REQ 全集
```

#### 6.3.3 链路识别（LLM 调用，成本需评估）

参考 spec2case `prd_knowledge_builder.py:47-58`：

```python
LINKAGE_PROMPT = """基于结构化需求，识别是否存在跨模块的业务链路。

判定标准（满足任一才输出）：
- 多个模块之间存在状态传递（模块 A 的操作结果影响模块 B 的可见性）
- 存在跨模块的数据一致性要求
- 存在端到端的业务流程闭环

每个链路输出：
- flow_hint：一句话描述完整链路（必须是可执行链路，不是泛泛的"跨模块联动"）
- flow_outline：2-5 步的链路骨架（入口/筛选/结果/操作/状态一致性）
- involved_modules：涉及的模块代号
- involved_requirements：涉及的 REQ 编号

约束：
- 一般输出 0-3 条链路，不要为了凑数量生成。
- 没有明确跨模块链路时输出 {"links": []}。

只输出 JSON。
"""
```

**成本影响**：新增 1 次 LLM 调用（可复用阶段 3 的稳定 system 前缀吃缓存）。在 `jobs>1` 场景下，链路用例生成可与模块用例并行。

#### 6.3.4 产物组织

新增 `cases/integration/` 目录存放链路用例，与模块用例分离：

```
output/<product>/<version>/cases/
├── cases/                     # 模块用例（现有）
│   ├── AUTH.json
│   └── CART.json
├── integration/               # 链路用例（新增）
│   └── LINK-001.json
├── traceability.json
└── 用例集.md
```

id 规范扩展：`TC-<MOD>-LINK-<SEQ>`，其中 `<MOD>` 取链路首个模块。

**schema 兼容**：`validator.ID_RE`（`validator.py:25`）当前为 `^TC-[A-Z][A-Z0-9_]*(-[A-Z0-9_]+)?-\d{3}$`，可匹配 `TC-AUTH-LINK-001`（`[A-Z0-9_]+` 匹配 `LINK`，`-001` 为序号）。**无需修改正则**。

#### 6.3.5 建议

**列为 P3，独立排期**。理由：

- 需要放宽 `stages.py:612-617` 的模块引用约束，触及流水线核心逻辑
- 新增 1+ 次 LLM 调用，成本影响需实测
- 收益（跨模块链路覆盖）显著，但属于**能力扩展**而非质量修复

---

## 第七部分：约束漂移检测

### 7.1 问题本质

已确认两处漂移（1.3 节）：

1. `design_method` 在 principles 标"必须"、在 04 标"可选"、示例里根本没有
2. `00-testing-principles.md` 声称注入阶段 3，实际未注入

**根因**：prompt 是自然语言资产，门禁是代码资产，二者靠人工同步，必然漂移。

### 7.2 约束登记表

新建 `docs/constraint-registry.md`，作为约束的唯一追踪源：

```markdown
| 约束 ID | 约束描述 | Prompt 出处 | 门禁实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| C-001 | id 格式 TC-<MOD>[-<SUB>]-<SEQ> | 00:67 | validator.py:57 | test_validator.py::test_id_format | ✅ |
| C-002 | module ∈ 模块表 | 00:67 | validator.py:64 | test_validator.py::test_module_enum | ✅ |
| C-003 | priority ∈ P0..P3 | 00:67 | validator.py:66 | — | ✅ |
| C-004 | P0 必带 @smoke | 00:57 | validator.py:73 | — | ✅ |
| C-005 | design_method 必须标注 | 00:26, 04:6 | validator.py:NEW | — | 🔧 待实施 |
| C-006 | 禁止不可验证预期 | 00:49 | validator.py:NEW | — | 🔧 待实施 |
| C-007 | 禁止重复用例 | 00:50 | validator.py:NEW | — | 🔧 待实施 |
| ... | | | | | |
```

### 7.3 自动漂移检测测试

**文件**：`tests/test_prompt_gate_alignment.py`

```python
"""约束漂移检测：断言 prompt 声明的关键约束都有门禁实现与测试覆盖。

任何一条约束若在 prompt 中强化但门禁未跟进，本测试必须失败。
"""
import re
from pathlib import Path

import pytest

from prd2tc import gates, validator
from prd2tc.prompt_parts import (  # 若采用方案 6.2
    COMMON_OUTPUT_RULES,
)

REPO = Path(__file__).resolve().parents[1]
PRINCIPLES = REPO / "prompts" / "00-testing-principles.md"
CASES_PROMPT = REPO / "prompts" / "03-test-cases.md"

# 关键约束 → 门禁实现的探测标记（函数体或模块级常量中应出现）
CONSTRAINT_MARKERS = {
    "design_method": "design_method",
    "不可验证预期": "VAGUE_EXPECTED_TOKENS",
    "重复用例": "case_signature",
    "P0 必带 @smoke": "@smoke",
}


@pytest.mark.parametrize("keyword,marker", list(CONSTRAINT_MARKERS.items()))
def test_prompt_constraint_has_gate_implementation(keyword: str, marker: str):
    """prompt 声明的关键约束必须在门禁代码中有对应实现。"""
    source = Path(validator.__file__).read_text(encoding="utf-8")
    assert marker in source, (
        f"prompt 约束「{keyword}」在 validator.py 中无实现标记「{marker}」；"
        f"请补充门禁规则或更新 CONSTRAINT_MARKERS"
    )


def test_stage3_example_contains_design_method():
    """阶段 3 的 JSON 示例必须包含 design_method —— 示例优先于描述。"""
    text = CASES_PROMPT.read_text(encoding="utf-8")
    match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text)
    assert match, "阶段 3 prompt 缺少 JSON 示例"
    example = match.group(1)
    assert "design_method" in example, (
        "阶段 3 JSON 示例缺少 design_method，"
        "模型会服从示例而非 principles 的 [必须] 要求"
    )


def test_principles_injection_scope_matches_reality():
    """00-testing-principles.md 声称的注入阶段必须与代码一致。"""
    text = PRINCIPLES.read_text(encoding="utf-8")
    claims_stage3 = "3 用例生成" in text.split("适用阶段")[1].split("\n")[0]
    stages_src = Path(gates.__file__).parents[0].joinpath("stages.py").read_text(encoding="utf-8")
    actually_injected = "load_principles" in stages_src.split("def stage_3_system")[1]
    assert claims_stage3 == actually_injected, (
        f"00-testing-principles.md 声称{'注入' if claims_stage3 else '不注入'}阶段 3，"
        f"但 stage_3_system {'注入了' if actually_injected else '未注入'}"
    )
```

### 7.4 边界与局限

**局限 1：字符串匹配脆弱**

门禁实现改名会导致误报。缓解：标记用**语义稳定**的字符串（常量名、错误信息关键词），改门禁实现时同步更新测试标记。

**局限 2：只能覆盖列出的约束**

`CONSTRAINT_MARKERS` 是人工枚举的。新增 `[必须]` 约束时若忘记登记，测试不会报警。

**缓解**：`test_prompt_constraint_has_gate_implementation` 用 `parametrize`，新增约束只需加一行。建议在 PR checklist 中加入"若修改 principles/04 的 [必须] 条目，必须同步 `CONSTRAINT_MARKERS`"。

**替代方案（更彻底）**：从 prompt 中正则提取所有 `[必须]` / `[禁止]` 条目，要求每条在 registry 中出现。缺点是自然语言条目无法自动对应代码，误报率高。**建议先用白名单方案，稳定后再考虑自动化。**

---

## 第八部分：实施路线图

### 8.1 改动清单总览

| 优先级 | 编号 | 改动 | 文件 | 风险 | 预估 |
|---|---|---|---|---|---|
| **P0** | A1 | 阶段 3 温度 0.5→0.3 | `stages.py` | 低 | 5min |
| **P0** | A2 | 示例补 `design_method` | `03-test-cases.md` | 低 | 10min |
| **P0** | A3 | 04 中移除"可选字段" | `04-case-constraints.md` | 低 | 5min |
| **P0** | A4 | `DESIGN_METHODS` 常量 | `schema.py` | 无 | 5min |
| **P0** | A5 | design_method 校验 | `validator.py` | **中** | 30min |
| **P0** | A6 | 不可验证预期检测 | `validator.py` | **中** | 45min |
| **P0** | A7 | 重复用例检测 | `validator.py` + `gates.py` | **中** | 45min |
| **P1** | B1 | 重复控制措辞消歧 | `00-testing-principles.md` | 低 | 15min |
| **P1** | B2 | 阶段 3 步骤对齐实际输入 | `03-test-cases.md` | 低 | 10min |
| **P1** | B3 | JSON 转义约束补充 | `03-test-cases.md` | 低 | 10min |
| **P1** | B4 | target 分级要求 | `00` + `validator.py` | 低 | 30min |
| **P1** | B5 | 阶段 2 门禁补强 | `gates.py` | 中 | 40min |
| **P1** | B6 | 覆盖维度统计 | `traceability.py` | 低 | 60min |
| **P2** | C1 | principles 注入声明修正 | `00-testing-principles.md` | 低 | 5min |
| **P2** | C2 | 金额格式校验 | `validator.py` | 低 | 20min |
| **P2** | C3 | 约束登记表 | `docs/constraint-registry.md` | 无 | 30min |
| **P2** | C4 | 漂移检测测试 | `tests/test_prompt_gate_alignment.py` | 低 | 60min |
| **P3** | D1 | prompt 模板片段化 | 新增 `prompt_parts.py` | 中 | 90min |
| **P3** | D2 | 链路用例层 | 多文件 | **高** | 3-5天 |

### 8.2 分阶段实施

#### 第一批（低风险，1 小时内完成）

**目标**：修复两处已确认的约束漂移，立即改善输出质量。

```bash
# 1. A1 温度
#    src/prd2tc/stages.py:53
#    _DEFAULT_TEMP = {1: 0.2, 2: 0.3, 3: 0.5}
_DEFAULT_TEMP = {1: 0.2, 2: 0.3, 3: 0.3}

# 2. A2 补示例 design_method（03-test-cases.md:18）
# 3. A3 04-case-constraints.md:6 移除「作为可选字段」
# 4. C1 00-testing-principles.md:10 修正注入声明
```

**验收**：`pytest -q` 全绿；`prd2tc generate --prd examples/sample-prd.md --stub` 正常产出。

> ⚠️ **当前工作区已有 6 个测试失败**（fixture 指向仓库外 `PRD_File/`），需先修复该问题再建立绿色基线。
>
> **v1.1 修订（R-6）**：v1.0 称「把 `tests/test_pipeline_stub.py:13`、`test_examples.py:12`、`test_constraints.py:9`
> 的 `SAMPLE_PRD` 改为 `REPO_ROOT / "examples" / "sample-prd.md"`」即可——**这不完整**。
> 这些测试还用 `tmp_path / "youxuan-mall" / "v0.3"` 硬编码了**产物目录**（product 取自 PRD 所在目录名，
> version 取自 PRD 内容），换 fixture 后 product 会变成 `examples`、version 变为 `v0.1`，断言仍会失败。
>
> 正确修法二选一：
> 1. **改断言**：把 `"youxuan-mall" / "v0.3"` 同步改为 `"examples" / "v0.1"`；
> 2. **显式传参**（推荐，断言不动）：在 `cli.main([...])` 里加 `--product youxuan-mall --version v0.3`，
>    让产物路径与现有断言保持一致。
>
> 另注：`test_constraints.py` 的 `SAMPLE_PRD` 仅用于 `test_stage_injection_with_examples` 等少数用例，
> 需逐个确认是否真的依赖它——无依赖的直接删除该常量更干净。

#### 第二批（门禁增强，2-3 小时）

**目标**：把 10 条未实现约束中的核心 3 条下沉为门禁。

顺序：**A4 → A5 → A6 → A7**

每条规则的验证方式：

```bash
# 构造负例，确认门禁能捕获
prd2tc validate --dir <含问题用例的临时目录>
# 预期输出应包含新增的错误信息
```

**测试补充**（`tests/test_validator.py`）：

```python
def test_design_method_required():
    errors = validate_case(BASE_CASE, 0, set())
    assert any("design_method" in e for e in errors)


def test_vague_expected_rejected():
    case = {**BASE_CASE, "design_method": "边界值分析",
            "expected": ["表现正常"]}
    errors = validate_case(case, 0, set())
    assert any("不可验证" in e for e in errors)


def test_duplicate_case_detected():
    seen = set()
    first = validate_case(BASE_CASE, 0, seen)
    sig = case_signature(BASE_CASE)
    assert sig  # 签名可计算
```

**验收**：负例被拦截；现有样例产物（若有）误报率 < 5%。

#### 第三批（prompt 修订 + 漂移防护，2 小时）

B1-B5 + C3-C4。完成后应具备**能力**：后续任何 prompt 约束强化，若门禁未跟进，`pytest` 直接失败。

#### 第四批（结构重构，独立评估）

D1 模板片段化为 D2 链路用例的前置。建议 D1 与 D2 之间留出独立评审窗口——模板化本身不产生功能收益，其价值在于降低 D2 的改动风险。

### 8.3 全局验收标准

| 维度 | 指标 | 目标 |
|---|---|---|
| 门禁覆盖率 | `docs/constraint-registry.md` 中 ✅ 占比 | 从 35% → 65% |
| 测试健康 | `pytest -q` | 全绿（需先修复既有 6 个失败） |
| 漂移防护 | 修改任一 `[必须]` 约束但不更新门禁 | 测试必须失败 |
| 可复现性 | `--repro` 模式 3 次结构指纹一致 | 保持 100% |
| 门禁有效性 | 构造负例，门禁拦截率 | 100% |
| 误报率 | 现有样例产物跑新门禁 | < 5% |
| **门禁与 prompt 一致性** | **prompt 自带示例必须通过门禁** | **100%（v1.1 新增，防止 R-1 类死循环）** |
| Token 成本 | 相同 PRD、相同模型的总 token | 不增加超 10% |

> **v1.1 新增验收项说明**：R-1 暴露的风险模式是「门禁比 prompt 示例更严」——
> 示例说 `expected = ["跳转首页"]`，门禁判它「过短」。这会让模型陷入
> 「照抄示例 → 被拦 → 改写 → 可能又踩空洞词表」的循环，白烧 token。
>
> 因此新增一条**必须自动化**的验收：把 `prompts/03-test-cases.md` 里的 JSON 示例抽出，
> 喂给 `validator.validate_case()`，断言**零错误**。任何门禁规则若拦掉 prompt 自带示例，
> 该测试立即失败。

### 8.4 风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| 新门禁误伤合法用例 | 中 | 先在样例产物上验证；提供白名单参数 |
| `design_method` 强制导致门禁频繁失败拖慢流程 | 中 | 观察实际通过率；若 >30% 失败，考虑降为 `--strict` 可选 |
| 温度降低导致用例多样性不足 | 低 | A/B 对拍；必要时模块级首次 0.4 / 重试 0.2 |
| 阶段 2 门禁补强导致方案频繁返工 | 中 | 先只加 Out Scope 检查（最明确），其余观察后再加 |
| 已有下游消费方依赖旧 schema | 中 | `design_method` 为新增可选字段，旧代码读取不受影响；但 `validate` 会报错，需 CHANGELOG 说明 |

---

## 附录 A：改动后的 validator.py 关键片段

```python
"""用例 JSON 校验（与 docs/case-template.md 字段规范一致）。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from prd2tc.schema import (
    ACTION_SCHEMA_VERSION,
    ACTIONS_BY_VERSION,
    DESIGN_METHODS,
)

MODULES = [...]          # 保持不变
PRIORITIES = ["P0", "P1", "P2", "P3"]
CASE_STATUSES = [...]
ACTIONS = sorted(ACTIONS_BY_VERSION[ACTION_SCHEMA_VERSION])
ID_RE = re.compile(r"^TC-[A-Z][A-Z0-9_]*(-[A-Z0-9_]+)?-\d{3}$")
REQ_RE = re.compile(r"^REQ-[A-Z][A-Z0-9_]*-\d{3}$")

# ---- 新增：约束实现标记（与 tests/test_prompt_gate_alignment.py 联动）----

# C-005 design_method 必须标注（00-testing-principles.md:26）
DESIGN_METHOD_ENUM = DESIGN_METHODS   # 实际判定用 schema 中的枚举

# C-006 禁止不可验证预期（00-testing-principles.md:49）
VAGUE_EXPECTED_TOKENS = (
    "表现正常", "显示正常", "功能正常", "运行正常", "工作正常",
    "无异常", "没有异常", "正常显示", "符合预期", "正确显示",
    "显示正确", "功能可用", "无报错", "不报错",
)
_MIN_EXPECTED_LEN = 8      # v1.2 按用户指定从严（v1.0 为 5、v1.1 为 3）；含数字的断言豁免
_VAGUE_MAX_LEN = 12        # 长度保护：长句通常含具体内容，不应判空洞

# C-007 禁止重复用例（00-testing-principles.md:50）
def case_signature(case: dict) -> str:
    """用例去重签名：模块 + 步骤序列 + 预期序列。"""
    def norm(text) -> str:
        return re.sub(r"\s+", "", str(text or "")).lower()
    steps = "|".join(
        f"{norm(s.get('action'))}>{norm(s.get('target'))}"
        for s in (case.get("steps") or []) if isinstance(s, dict)
    )
    expected = "|".join(norm(e) for e in (case.get("expected") or []))
    raw = f"{norm(case.get('module'))}#{steps}#{expected}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def validate_case(case, index, seen_ids, allowed_modules=None, *,
                  require_design_method=True) -> list[str]:
    """校验单条用例，返回错误列表（尽可能收集全部问题）。"""
    errors: list[str] = []
    at = f"[用例#{index + 1} {case.get('id', '(无id)')}]"

    # ... 原有 id / module / priority / status / tags 校验保持不变 ...

    # C-005：design_method
    if require_design_method:
        method = case.get("design_method")
        if not method:
            errors.append(f"{at} 缺少 design_method（必须标注测试设计方法）")
        elif str(method) not in DESIGN_METHOD_ENUM:
            errors.append(
                f"{at} design_method 非法: {method}；可选值 {sorted(DESIGN_METHOD_ENUM)}"
            )

    # ... requirement / steps 校验保持不变 ...

    # C-006：expected 空洞断言
    expected = case.get("expected")
    if not isinstance(expected, list) or len(expected) == 0:
        errors.append(f"{at} expected 为空")
    else:
        for item in expected:
            text = str(item).strip()
            if not text:
                errors.append(f"{at} expected 含空字符串")
                continue
            if len(text) <= _VAGUE_MAX_LEN and any(t in text for t in VAGUE_EXPECTED_TOKENS):
                errors.append(
                    f"{at} 预期不可验证（空洞断言）: {text!r}；"
                    f"请改为可断言的表述（具体 UI 元素/数据/状态/金额）"
                )
            elif len(text) <= _MIN_EXPECTED_LEN and not any(ch.isdigit() for ch in text):
                errors.append(f"{at} 预期过短且无具体值: {text!r}；请补充具体断言对象")

    # ... title 校验保持不变 ...
    return errors
```

---

## 附录 B：与 spec2case 的设计对照

| 维度 | spec2case | prd2tc（现状） | 本方案目标 |
|---|---|---|---|
| 阶段 3 温度 | 0.3 | 0.5 | 0.3 |
| prompt 分块策略 | 0.1（最冷） | 无分块 | 可选（P3） |
| 约束可验证性 | block_roles 唯一归属等硬约束 | 35% 覆盖 | 65% 覆盖 |
| 重复控制措辞 | 明确"相似≠重复"+例外 | "禁止同质"（未定义） | 可判定三要素 |
| 链路用例 | integration_lu（≥2 模块） | 无 | P3 可选 |
| 输出格式 | Markdown 表格 | **JSON + 强校验** | **保持 JSON** |
| 字段枚举 | 用例类型不设枚举 | 13 action 硬枚举 | **保持硬枚举** |
| 人工答案回流 | 融合为 final_prd | 仅覆盖写 | 独立议题 |

> **说明**：输出格式与字段枚举两项，prd2tc 的路线更优（产物供 Playwright 机器消费），不应向 spec2case 看齐。

---

## 附录 C：参考资料

- spec2case：<https://github.com/blackhaiyu-sudo/spec2case>（`langgraph_text_pipeline/graph.py`、`agents/qa_agents/templates/`）
- CANDOR / Hallucination to Consensus：<https://arxiv.org/abs/2506.02943>（ASE 2025，多 agent 共识消幻觉）
- Do LLMs Generate Useful Test Oracles?：<https://homes.cs.washington.edu/~mernst/pubs/neurosymbolic-oracles-ase2025-abstract.html>（ASE 2025，无偏数据集，LLM 变异分数 43% vs 人工 45%）
- TestBrain：<https://github.com/MangoFisher/TestBrain>（RAG 路线参考，**未采用**）

---

*文档结束。实施过程中如发现方案与实际代码不符，以代码为准并更新本文档。*