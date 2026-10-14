# 用例字段与规则契约（case-spec.md）

> 本文是 prd2tc **输出侧的唯一规范**：用例 JSON 字段、动作词表、`target` 写法、优先级与冒烟集判定、
> 重复判定、追踪矩阵与导出渲染，均以本文为准。本文整合了历史用例字段说明，并按当前代码复核。
> 文中 `路径:行号` 为核验过的当前行号，常量取值以代码为准；未能验证的结论在正文或附录 A 标注。

| 契约面 | 单一事实源 | 消费者 |
| --- | --- | --- |
| 动作词表 | `src/prd2tc/actions.py`（`ACTION_SPECS`） | `schema.ACTIONS_BY_VERSION`、注入提示词、validator、导出 |
| Schema 与枚举 | `src/prd2tc/schema.py` | validator、stages、pipeline |
| 字段与门禁 | `src/prd2tc/validator.py`（`validate_case`/`validate_dir`） | 阶段 3 批次门禁、`prd2tc validate` |
| 优先级与冒烟集 | `src/prd2tc/priority.py`（`grade`/`regrade`/`select_smoke`/`apply_smoke`） | 落盘前统一重判 |
| `target` 契约校验 | `src/prd2tc/prototype.py`（`validate_targets`，仅在有 UI 契约时生效） | 阶段 3 批次门禁 |
| 导出渲染 | `src/prd2tc/nlsteps.py` | Excel / XMind / Markdown 评审稿 |

## 1. 用例 JSON 的完整字段

### 1.1 顶层容器

```json
{ "schemaVersion": "2.0", "actionSchemaVersion": "1.1", "cases": [ /* 用例对象 */ ] }
```

| 键 | 值 | 说明 |
| --- | --- | --- |
| `schemaVersion` | `"2.0"`（`schema.py:8`） | 字段契约版本 |
| `actionSchemaVersion` | `"1.1"`（`schema.py:13`） | 动作词表版本 |
| `cases` | 用例数组 | 落盘按模块拆分：`cases/cases/<MODULE>.json`（`io.save_cases`） |

**读取兼容**：`validate_dir` 与 `schema.normalize_cases` 接受**旧版顶层数组**及缺 `actionSchemaVersion`
的 `{cases:[...]}` 对象（`validator.py:234-239`、`schema.py:28-33`），历史文件无需迁移。

### 1.2 用例字段表

「必填」区分**生成时必填**（`validate_case` 默认参数下拦截）与**读取旧产物**的兼容路径。

| 字段 | 生成时必填 | 读取旧产物 | 类型 | 约束与说明 |
| --- | --- | --- | --- | --- |
| `id` | ✅ | ✅ | string | 匹配 `ID_RE`；全局唯一（跨文件查重，`seen_ids`） |
| `module` | ✅ | ✅ | string | ∈ 允许模块集合；`id` 前缀必须与之相等（`renumber` 强制，`stages.py:828-832`） |
| `subModule` | — | — | string | 子模块名，仅用于分组/导出，**无枚举门禁**（如 `LOGIN`、`ENABLE_TOGGLE`） |
| `title` | ✅ | ✅ | string | 非空；动宾结构一句话 |
| `design_method` | ✅ | ⚠ 可放宽 | string | ∈ `DESIGN_METHODS`（6 值）；`validate_case(require_design_method=False)` 与 `validate_dir` 同名开关可读历史用例 |
| `priority` | ✅ | ✅ | string | ∈ `PRIORITIES` = P0/P1/P2/P3；**落盘前被 `regrade` 确定性重判覆盖** |
| `tags` | ✅ | ✅ | string[] | 非空；`@smoke` **只能**出现在 P0 上；`@regression` 为约定（无门禁） |
| `requirement` | ✅ | ✅ | string[] | 非空；每项匹配 `REQ_RE`；阶段 3 另校验「属于本模块真实 REQ」（`stages.py:981-984`） |
| `preconditions` | — | — | string[] | 前置条件；无法扁平化的复杂数据写这里 |
| `testData` | — | ✅ | string[] | 「键=值」字符串数组，值只能标量；旧版 object 容器可读（§6） |
| `steps` | ✅ | ✅ | object[] | 非空；每步见 §1.3；**至少 1 个断言动作**（§5） |
| `expected` | ✅ | ✅ | string[] | 非空；逐条过质量门禁（§7） |
| `createdBy` | — | — | string | 历史字段：**`src/` 零引用**（不校验、不导出） |
| `status` | — | — | string | 缺省落盘补 `active`（`io.py:119`）；存在则必须 ∈ `CASE_STATUSES`（§11） |
| `prioritySource` | 系统写 | — | string | 恒为 `"graded"`：该优先级来自确定性重判 |
| `priorityBasis` | 系统写 | — | object | `{requirement, dimension}`：重判依据 |
| `modelPriority` | 系统写 | — | string | 仅当模型原判 ≠ 重判结果时写入，供审计对比 |
| `smokeReason` | 系统写 | — | object | 仅冒烟集内用例：`{priority, dimension}` |

### 1.3 `steps[]` 字段

| 字段 | 必填 | 类型 | 约束 |
| --- | --- | --- | --- |
| `action` | ✅ | string | ∈ `ACTION_NAMES`（23 个，§3） |
| `target` | ✅ | string | 非空（§4）；`reload`/`waitFor` 可省略，管线补 `当前页面`/`页面就绪`（`stages.py:283-292`） |
| `data` | — | string | 必须是字符串/标量，禁止嵌套对象或数组（`schema.validate_step_data`） |

### 1.4 为什么这么定

- **两级宽松度是有意的**：生成时严（`design_method` 必填、必须有断言、预期必须可断言），
  读取时宽（旧产物能进追踪矩阵、能导出）。门禁收窄的是**新输出**，不是历史资产。
- **`subModule` 不做枚举**：模块划分随 PRD 变化，硬编码子模块表会与真实 PRD 冲突；它只影响导出分组。
- **审计字段由系统写、不由模型写**，使「为什么是这个优先级/为什么进冒烟集」可回查。
  实测 `outputs/7e84a0d3…`（80 条）：`prioritySource` 100% 为 `graded`，其中 11 条（13.8%）带
  `modelPriority`，即模型原判与重判不一致。

## 2. 模块代号与用例 id

**通用模块表**（`validator.MODULES`，`validator.py:20-24`）：电商域 `AUTH` `HOME` `PROD` `SEARCH` `CART`
`CHECKOUT` `COUPON` `ORDER` `ACCOUNT` `COMMENT`（账号/首页/商品/搜索筛选/购物车/结算/优惠券/订单/个人中心/评价）；
通用 `COMMON` `PERF`（公共兜底/性能与兼容）；能力码 `CHK` `GLB` `DET` `LIST` `ERR`（校验/全局/详情/列表/异常）。

> `MODULES` 只是**兜底白名单**：`validate_case(..., allowed_modules)` 传入时以传入集合为准
> （`validator.py:140`），`prd2tc validate` 未传时用 `MODULES`（`cli.py:226`）。

**实际生效的是动态模块**（`profiles.py:1`「不限制动态模块发现」）：真实产物里的模块是
`KB/SCENE/AGENT/TOOL/TEST`（`outputs/56b4686c…`：31 条需求 / 5 个模块），不在 `MODULES` 内仍全部通过校验，
因为门禁拿到的是本模块代号集合（`stages.py:861` 的 `allowed = {module}`）。
下发给模型的模块约束原文是「2~8 位大写，须与 id 前缀 / REQ 前缀一致」（`stages.py:135`）。

**id 格式**（`ID_RE`，`validator.py:28`）：`^TC-[A-Z][A-Z0-9_]*(-[A-Z0-9_]+)?-\d{3}$`，
即 `TC-<MOD>[-<SUB>]-<3位序号>`：`<MOD>` 首字符必须字母、可含数字与下划线；`<SUB>` 可选且**最多一段**；
序号**恰好 3 位**。`requirement` 用 `REQ_RE = ^REQ-[A-Z][A-Z0-9_]*-\d{3}$`（`validator.py:29`）。

| 输入 | 判定 | 原因 | 输入 | 判定 | 原因 |
| --- | --- | --- | --- | --- | --- |
| `TC-AUTH-LOGIN-001` | ✅ | MOD+SUB | `TC-ABC-DEF-GHI-001` | ❌ | SUB 只能一段 |
| `TC-KB-020` | ✅ | 无 SUB | `TC-AGENT-1` | ❌ | 序号须 3 位 |
| `TC-A-001` / `TC-A1-002` / `TC-A_B-001` | ✅ | MOD 可单字母/含数字/含下划线 | `TC-KB-0200` | ❌ | 序号恰好 3 位 |
| `TC-A-001-001` | ✅ | 第二段当 SUB | `TC-kb-20` | ❌ | 只允许大写 |
| `REQ-KB-009` / `REQ-A_B-001` | ✅ | — | `KB-009` / `REQ-KB-9` | ❌ | 缺前缀 / 序号位数 |

**真实生成路径的 id 由程序重编号**：`renumber()` 写死 `TC-{module}-{i:03d}` 并强设 `module`
（`stages.py:828-832`），与优先级重判同构，属确定性强制、不依赖模型自觉。
因此真实产物极少出现 SUB 段，`subModule` 是独立字段而非 id 的一部分。

## 3. 动作词表（23 个）

权威定义是 `actions.py` 的 `ACTION_SPECS`：`ActionSpec(name, kind, summary, needs_data)`，
`kind` ∈ `operate`（改变系统状态）/ `assert`（判定通过/失败）。

### 3.1 操作用（12 个，`actions.py:39-52`）

| 动作 | 语义（注入提示词的原文） |
| --- | --- |
| `goto` | 打开/切换到某页面 |
| `fill` | 在输入框填写内容（需 data） |
| `click` | 点击元素 |
| `selectOption` | 在下拉/选择器中选择选项（需 data） |
| `check` | 勾选复选框/单选（**仅用于可勾选元素**） |
| `uncheck` | 取消勾选复选框 |
| `upload` | 上传文件（点击选择或拖拽到上传区）（需 data） |
| `hover` | 鼠标悬停在元素上 |
| `press` | 在元素上按键（如 Enter/Esc）（需 data） |
| `reload` | 刷新页面 |
| `scrollTo` | 滚动到元素位置 |
| `waitFor` | 等待元素出现或状态就绪（**不是断言**，其后仍需 `expect*`） |

### 3.2 断言用（11 个，`actions.py:55-67`）——每条用例至少 1 个

| 动作 | 语义 |
| --- | --- |
| `expectVisible` | 断言元素可见 |
| `expectNotVisible` | 断言元素不可见/不存在（含关闭弹窗、排他） |
| `expectText` | 断言元素文案等于给定值（需 data） |
| `expectValue` | 断言输入框当前值（需 data） |
| `expectEnabled` | 断言元素可交互（按钮可点等） |
| `expectDisabled` | 断言元素禁用（按钮置灰等） |
| `expectChecked` | 断言复选框/开关为选中态 |
| `expectUnchecked` | 断言复选框/开关为未选中态 |
| `expectState` | 断言通用状态（激活态/展开态/on-off 等）（需 data） |
| `expectCount` | 断言元素或列表项数量（需 data） |
| `expectUrl` | 断言当前地址/页面标识（需 data） |

通常需要 `data` 的动作共 9 个（`ACTIONS_WITH_DATA`，`actions.py:76`）：`fill` `press` `selectOption`
`upload` `expectText` `expectValue` `expectState` `expectCount` `expectUrl`。
门禁**不强制**它们带 data（有些场景确实无需值）。

### 3.3 为什么补齐词表：`check` 的误用率 87%~91%

原词表只有 12 个动作，`check` 是**唯一听起来像「验证」**的词，于是被当成万能断言。
逐条枚举 47 次 `check` 后的性质是「**没有更合适的动作可用**」，而不是模型乱来：

| 类别 | 次数 | 缺什么动作 | 典型写法 |
| --- | ---: | --- | --- |
| 元素状态 / 位置 | 23 | `expectDisabled`/`expectState` | `主提示词页签激活态 (tab-main-prompt.active)` |
| 拖拽上传文件 | 9 | `upload` | `拖拽入 1048575 字节 txt 文件 (data-testid=field-dropzone)` |
| 网络请求 / 数量 | 5 | `expectCount` | `保存接口请求次数 (network-save-draft)` |
| 排他 / 反向断言 | 4 | `expectNotVisible` | `工具详情区无完整JSON编辑框` |
| 本应 `click`/`hover` | 4 | 已有动作但选错 | `发起整体命中测试` |
| 本应 `expectVisible`/`expectText` | 2 | 已有动作但选错 | `召回结果包含该文档的片段` |
| **复选框勾选（唯一正确语义）** | **0** | — | — |

> **41/47 = 87% 源于词表缺口**，仅 6 条是选错已有动作；评测 v2 的 23 次 `check` 中误用 21 次 = **91%**。
> 来源：逐条枚举；评测方法与复核原则见 [quality-evaluation.md](quality-evaluation.md) §5.2–§5.3。

**本次核验（统计真实产物的 `action` 频次）**：

| 产物 | 用例数 | `check` | `expectState` | `upload` | `expectCount` | `expectDisabled` | `expectNotVisible` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| v1（`c653dc92`） | 94 | **47** | 0 | 0 | 0 | 0 | 0 |
| v2（`5b526ce6`） | 121 | **23** | 0 | 0 | 0 | 0 | 0 |
| v3（`56b4686c`） | 71 | **5** | 0 | 0 | 0 | 0 | 0 |
| 修复后（`7e84a0d3`） | 80 | **0** | 15 | 6 | 17 | 1 | 25 |

前三次运行里四个新动作**全部为 0**，与缺口判断一致；补词表后的产物里它们被真实启用，同时 `check` 归零 ——
这是「补齐词表比反复强调『不要滥用 check』更有效」的直接证据。

**`waitFor` 是同步动作、不是断言**：它属 `_OPERATIONS`（`actions.py:51`），`is_assertion("waitFor")`
恒为 `False`（`actions.py:79-81`）；它不表达期望值，断言存在性门禁不认它。错误认知有实测代价：
v1 的 5 条无断言用例中 `TC-AGENT-013`、`TC-KHTEST-008` 靠「超时即失败」勉强可判定，却仍无法表达期望。

**单一事实源与版本演进**：`ACTION_SPECS` 是唯一权威，`schema.ACTIONS_BY_VERSION[ACTION_SCHEMA_VERSION]`
由它派生（`schema.py:20`），注入提示词的动作清单由 `render_actions_for_prompt()`（`actions.py:91-101`）
生成、**不再手写**，阶段 3 的 `field_spec()` 直接拼接它（`stages.py:145-146`）——动作枚举曾散落 6 处，
任一处改动都会漂移。版本 `ACTION_SCHEMA_VERSION` 由 **1.0 → 1.1**（`schema.py:13`）：1.0 是 12 个动作，
1.1 是 23 个且**必须是旧版本的超集**（本次核验：`ACTIONS_BY_VERSION["1.0"] <= ACTIONS_BY_VERSION["1.1"]` 为真），
新增 11 个：`uncheck` `scrollTo` `upload` `expectValue` `expectEnabled` `expectDisabled` `expectChecked`
`expectUnchecked` `expectState` `expectCount` `expectNotVisible`。超集意味着旧产物无需迁移仍可通过校验。

## 4. `target` 书写规范

```
页面元素中文名 (种类=真实取值)      ← 有 UI 契约时的唯一推荐写法
页面元素中文名 种类=真实取值        ← 无括号变体，等价
页面元素中文名                     ← 无 UI 契约时只写中文名
```

| 种类 | 取值来源（`prototype._annotation_pools`，`prototype.py:645-673`） | 校验强度 |
| --- | --- | --- |
| `id` | 元素 `id` 锚点（如 `docName`、`retrieveQuery`） | **严格** |
| `class` | 元素 `class` 锚点（如 `modal`、`file-drop`） | **严格** |
| `page` | 导航页键或页面标签（如 `knowledge`、`config`） | **严格** |
| `tab` | 页签键或页签标签（如 `main`、`tools`、`skills`） | **严格** |
| `text` | 可见文案池：按钮、弹窗标题与按钮、提示文案、字段 label/placeholder | 宽松 |
| `name`/`label`/`placeholder` | 归一到 `text`（`_KIND_ALIASES`，`prototype.py:631-634`） | 宽松 |
| `data-testid`/裸标识符 | 归入 `any` = 全部 id ∪ class 锚点（`_STRICT_KINDS` 含 `any`） | **严格** |

**为什么「种类必须与契约一致」**：`tools` 在契约里确实存在，但它只是**页签键**（`tabs[].key`），
不是任何元素的 `id`。早先只校验「值是否存在」而不分种类，跨命名空间误用被全部放行。
本次核验（对 v2 的 121 条用例重跑 `validate_targets`，契约取自该次运行的 `analysis/ui-spec.json`）
复现了该缺陷：**18 处错误中 17 处是种类错配**——`id=tools`×9、`id=skills`×5、`id=main`×3
（另有 1 处引用了契约中不存在的标识符）。真实报错原文（`prototype._check_annotation`）：

```
[用例 TC-KB-020] target 的 `id=tools` 不是契约中的真实 id（契约有 20 个：
['app', 'chat', 'count', 'docFile', 'docName', 'fullText', 'modal', 'nameCount', 'nav', 'promptEditor']…）；
请改用同种类的真实取值
```

实现见 `prototype.py:697-710`（严格种类逐池比对）与 `prototype.py:685-696`（未知种类宽松比对，
取值不存在时提示可选项 `id=`/`class=`/`page=`/`tab=`/`text=`）。**报错必须带可选项**，否则模型不知往哪改。

**无括号变体必须同时支持**：`知识库配置页 page=knowledge` 与 `(page=knowledge)` 等价
（`nlsteps.split_target` `nlsteps.py:80-85`；门禁侧同一识别在 `prototype.py:748-754`）——
否则模型换个写法即可绕过校验。**无 UI 契约时只写中文元素名**，门禁不做契约校验；这是降级路径而非推荐路径：
v1 在无契约条件下产出的 143 个标识符 **0 个真实**、7 种 URL 路径 **0 个存在**
（相关背景见 [ingestion.md](ingestion.md)），target 全部不可执行。

单页原型（`spec.has_url_routing` 为假）另有两条拦截（`prototype.py:755-771`）：`target` 里出现 `/xxx` 路径时报
「原型是单页应用、不存在 URL 路由；`goto` 应使用页面键 [...]」；`goto` 的 `data` 以 `/` 开头或落在未知页面时报错。

> ⚠ 未验证：契约校验只在提供 UI 契约时生效；本文未核验「target 合规 ⇒ 能被 Playwright 跑通」这一更强命题。

## 5. 断言存在性门禁

**规则**：每条用例的 `steps` 中至少 1 个 `action` ∈ `ASSERTIONS`
（`validator.py:192-197`，判据 `actions.is_assertion`）。真实报错原文（移除一条合规用例的全部 `expect*` 步骤）：

```
缺少断言步骤：用例必须至少包含一个断言动作
（expectChecked/expectCount/expectDisabled/expectEnabled/expectNotVisible/expectState/expectText/expectUnchecked/expectUrl/expectValue/expectVisible）；
仅用 click/fill/goto/waitFor 无法判定通过或失败
```

**为什么报错必须列出可选项**：只报「缺少断言」而不给菜单，模型无从下手；把 11 个合法断言连同
「仅用 click/fill/goto/waitFor 无法判定」的原因一起回灌，下一次输出才有确定方向。同一原则贯穿本文所有门禁
（未知 `action` 列出全部 23 个可选值、`design_method` 列出 6 个、冒烟集报错给出上限算式）。

**为什么要有这条门禁**：此前契约只约束 `expected` 的**文本质量**，不约束 `steps` 里存在断言，
于是「只有 `goto/fill/click`、无任何判定依据」的用例完全合规。实测这类用例（按 `is_assertion` 逐条复算）：
v1 **5 条**（`TC-AGENT-011/013/017`、`TC-KHTEST-008`、`TC-SCENE-006`）、v2 **4 条**、v3 **1 条**、修复后产物 **0 条**。

## 6. `testData` 契约

| 形态 | 判定 | 说明 |
| --- | --- | --- |
| `["phone=13800138000","password=demo123456"]` | ✅ 推荐 | 「键=值」字符串数组 |
| `["retrieveQuery=入住手续"]` | ✅ | 单元素同样合法 |
| `"k=v"`（裸字符串） | ✅ 可读 | `flatten_test_data` 视为单元素数组（非推荐写法） |
| `None` / 缺省 | ✅ | 归一化为 `[]` |
| `{"a":1}`（旧版 object，值为标量） | ⚠ 可读 | 容器不判错，读取时归一化为 `["a=1"]` |
| `{"doc":{"name":"x"}}` | ❌ | `testData.doc 是嵌套对象/数组，必须改为「键=值」标量（复杂数据请写进 preconditions）` |
| `[{"a":1}]` | ❌ | `testData[0] 是嵌套对象/数组，必须改为「键=值」字符串（复杂数据请写进 preconditions）` |
| `123` | ❌ | `testData 类型非法: int（应为「键=值」字符串数组）` |

约束实现 `schema.validate_test_data`（`schema.py:83-97`）；归一化 `schema.flatten_test_data`
（`schema.py:45-73`）：布尔值输出小写（`k=true`），旧版嵌套值降为**紧凑 JSON 文本**（`k={"a":1}`）以免丢信息。
`steps[].data` 同源：必须是字符串/标量，嵌套时报 `steps[6].data 必须是字符串/标量，不能是嵌套对象或数组`
（`schema.py:100-111`）。

**为什么必须扁平**：① 嵌套是 JSON 转义崩坏的首要来源——实测 MiniMax-M3 因 `testData` 嵌套在思考里反复纠错，
整轮 **26,504 字符输出 100% 是推理、可用产出为 0**（`schema.py:41-43`、`markdown_transit.py:5-7`）；
② 复杂数据有更好的去处：`preconditions` 是自由文本，不需要转义；
③ 校验器刻意**只报告、不归一化**（`validator.py:216-217`），归一化由管线在落盘前完成
（`stages.py:295-312`），使门禁看到的形态与最终产物一致，而嵌套这种会写坏 JSON 的形态必须被打回重出。

## 7. `expected` 质量门槛

| 规则 | 判据（`validator.py:41,48,74-78`） | 真实报错原文 |
| --- | --- | --- |
| 非空 | 必须是 list 且非空，元素 strip 后非空 | `expected 为空` / `expected 含空字符串` |
| 拒绝空洞表述 | 命中 `VAGUE_EXPECTED_TOKENS` **且** `len(text) <= 12`（`_VAGUE_MAX_LEN`） | `预期不可验证（空洞断言）: '表现正常'；请改为可断言的表述（具体 UI 元素/数据/状态/金额）` |
| 拒绝过短 | `len(text) <= **8**`（`_MIN_EXPECTED_LEN`）**且不含任何数字** | `预期过短且无具体值: '跳转首页'；请补充具体断言对象` |

`VAGUE_EXPECTED_TOKENS`（15 个，`validator.py:36-40`）：`表现正常` `显示正常` `功能正常` `运行正常`
`工作正常` `无异常` `没有异常` `正常显示` `符合预期` `正确显示` `显示正确` `功能可用` `无报错` `不报错`。

两处**防误伤豁免**（均已实测）：**长度豁免**——`len(text) > 12` 时 `is_vague_expected` 直接返回 `False`，
「页面跳转正常，提示「余额不足」」含具体内容，不应因出现「正常」被判错；**数字豁免**——`len <= 8` 但含数字时放行
（实测 `['共3条']` 不报错）。**门槛取 8 是有意从严**：把「页面渲染」「合计归零」「跳转首页」这类 4~6 字短语挡在门外，
要求预期写成完整可断言的句子并写明**可观测对象**（具体页面/元素/字段/状态/数值）；代价是实测 18 条真实样本
**拦截率 38.9%**（含示例库 2 条），因此示例库、prompt 示例与文档必须同步改写，否则模型照抄示例即被拦，
形成「示例 → 门禁 → 重试」死循环（见 [prompts-and-gates.md](prompts-and-gates.md) §6）。

| 不合格（会被拒） | 合格 |
| --- | --- |
| `成功`、`正常`、`通过`、`无异常`、`页面渲染`、`跳转首页` | `登录成功后跳转至首页`、`页面成功渲染并显示预期内容` |
| — | `删除后小计与合计金额均归零`、`导航栏显示当前用户昵称`、`数量 1→2 且小计同步更新` |

## 8. `design_method` 枚举与「设计方法 × 测试维度」

| 枚举值（`DESIGN_METHODS`，`schema.py:25`） | 触发条件（`00-testing-principles.md` §2） | 对维度推断的作用 |
| --- | --- | --- |
| `等价类划分` | 输入存在取值域 | 无直接映射 |
| `边界值分析` | 输入存在取值域 | → **边界**（`_BOUNDARY_METHODS`，`priority.py:61`） |
| `判定表` | 多条件组合影响同一结果 | 无直接映射 |
| `状态迁移` | 存在状态机/状态切换 | 无直接映射 |
| `场景法` | 存在业务流程/用户旅程 | 无直接映射 |
| `错误推测` | 历史缺陷高发点/易错点 | → **反向**（`_NEGATIVE_METHODS`，`priority.py:62`） |

`design_method` 是维度的**首选依据**（模型显式标注、可审计），文本特征作补充；判定顺序固定为
**异常 > 边界 > 反向 > 正向**（`priority.infer_dimension`，`priority.py:110-129`）：
① `_EXCEPTION_RE` 命中 → 异常；② `design_method == 边界值分析` 或 `_BOUNDARY_RE` 命中 → 边界；
③ `design_method == 错误推测` 或 `_NEGATIVE_RE` 命中 → 反向；④ 否则 → 正向。
两个易错点已在代码留注记：「拒绝/拦截/非法」刻意归**反向**而非异常（它们描述系统正确拒绝非法输入，
不是服务端故障）；`错误(?!提示|文案|信息)` 用否定前瞻排除「错误提示文案」这类校验反馈，否则会被错误降级。

## 9. 优先级规则

判定模型：`用例优先级 = 需求优先级（业务重要性） × 测试维度修正`，并设**数据安全例外**。
完整判定表（`_GRADE_TABLE`，`priority.py:46-53`，已逐格复算）：

| 维度 \ 需求 | P0 需求 | P1 需求 | P2 需求 |
| --- | --- | --- | --- |
| 正向（主流程走通） | **P0** | P1 | P2 |
| 反向（校验 / 拒绝 / 确认） | **P0** | P1 | P2 |
| 边界（上限 / 空值 / 恰好） | P1 | P2 | P2 |
| 异常（失败 / 不可用 / 容错） | P1 | P2 | P2 |

补充规则（`grade`/`regrade`，`priority.py:226-277`）：**P3 需求**一律维持 P3；需求优先级非法或缺失按 `P1` 处理；
**多需求用例取最高优先级**（保守，不因关联次要需求而降级）；重判写 `prioritySource="graded"` 与
`priorityBasis={requirement,dimension}`，模型原判与结果不一致时把原值存入 `modelPriority`。

**数据安全例外**（`_is_safety`，`priority.py:88-90,132-139`）：涉及写操作防护的用例即使维度是边界/异常也**不降级**，
判定范围为 `title` + 需求标题 + `expected`，词表：`保存|落盘|发布|删除|移除|确认删除|确认移除|取消删除|上传|类型|格式|1MB|MB|启用|开关|持久化|覆盖|替换|连接|绑定`。
本次核验（对 v3 的 71 条产物 + 需求优先级复算 `grade`）：共 **11 条**命中例外并保持原级，其中 3 条为 P0 需求的边界用例、
1 条为 P0 需求的异常用例。实例：`TC-AGENT-004`（P0 需求 × 异常，命中 `保存`/`发布` → P0，否则 P1）、
`TC-KB-006`（P0 × 边界，命中 `保存`/`1MB` → P0）、`TC-KB-009`（P0 × 边界，命中 `1MB` → P0）、
`TC-KB-019`（P1 × 边界，命中 `启用`/`开关` → P1，否则 P2）。`类型|格式|MB` 这类宽泛词会让较多上传校验用例落进例外，
这是**有意保守**：漏判（把数据安全用例降级）的代价远高于多判。

**为什么不是「边界/异常一律 P2」**：`priority.py:18-26` 记录该曾评估方案会整体降级边界/异常，
而其中「编辑后保存落盘」「删除文档二次确认」「上传类型/大小校验」**破防即数据错误或丢失**，与「次要」完全不符；
且边界/异常恰是缺陷发现率最高的一类。

> ⚠ **未复现的一处数字**：`priority.py:20` 记录的实测口径为「71 条真实用例下 P0=1(1%) / P2=50(70%)」。
> 本次在 v3 的 71 条产物上按两种解释复算都得不到该组数字：① 仅「边界/异常 → P2」得 P0=16/P1=28/P2=27；
> ② 「非正向 → P2」（把反向也降级）得 P0=3/P1=20/P2=48（P2 占 67.6%）。②与记录值最接近（P2 逼近 50、P0 仅个位数），
> 但**不能确认记录值的确切口径**。结论方向（该方案会让 P2 一家独大、P0 近乎消失）成立。

**维度推断必须先剥离领域名词**：`strip_domain_noise`（`priority.py:83,93-107`）在关键词匹配前删掉
`_DOMAIN_NOISE = ("提示词",)`。因为「提示词」字面含「提示」，若词表写成宽松的裸 `提示`，
「主提示词编辑器」这类**主干正向用例**会被判成反向/校验，污染优先级与冒烟集选取（实测 v3 的 P0 中多数因此被错分，
使「主干正向」只剩 1 条，冒烟集无从选取）。本次核验：v3 的 71 条中有 15 条在 title/expected 里提到「提示词」，
用**未剥离**文本匹配当前 `_NEGATIVE_RE` 时其中 5 条（`TC-AGENT-006/007`、`TC-SCENE-014/016/018`）会被判为反向。
当前词表已用 `提示(?:文案|信息|弹窗|语)` 限定，故该函数对现有词表是冗余的——它是**防御性归一化**，
防止词表被改回宽松写法后领域名词与通用关键词互相干扰。

## 10. 冒烟集（`@smoke`）

**语义**：部署后最先跑的**最小**集合，回答「这个版本还能不能继续测」。价值来自「小」，规模逼近全量即失去意义。
选取规则（`priority.select_smoke`，`priority.py:163-198`）：

| # | 约束 | 实现 |
| --- | --- | --- |
| 1 | 必须是 **P0**（必要条件，非充分条件） | `priority != "P0"` 直接跳过 |
| 2 | **排除边界与异常维度** | `infer_dimension(case) in (边界, 异常)` 跳过 |
| 3 | **每个需求最多 1 条** | 按排序依次取，需求的 REQ 已被占用则跳过；无 `requirement` 的用例不占配额 |
| 4 | **总量上限 = min(25, 20% × 总数)** | `SMOKE_MAX = 25`、`SMOKE_RATIO = 0.20`（`priority.py:158-159`）；`limit = max(SMOKE_MIN=1, min(25, int(total*0.2)))` |

选取顺序完全由数据决定：先按「正向优先 → 模块 → id」排序，再逐条取「每需求 1 条」，最后按上限截断，
因此同一输入必然得到同一结果、`--repro` 下可复现，也不受模型是否自标 `@smoke` 影响。
`apply_smoke`（`priority.py:201-223`）会**移除**不符合规则的 `@smoke`（如标在边界用例或 P1 上），
故产物里的 `@smoke` 恒等于「冒烟集」。**顺序很重要**：必须先完成全部优先级重判再选冒烟集
（`priority.py:276` 在 `regrade` 末尾调用），否则会基于旧优先级挑选（实测踩到：先选后判导致冒烟集偏大）。
阶段 3 提示词明确要求模型**不要输出** `@smoke`（`stages.py:141-142`），由系统统一判定。

**校验器的方向是「只有 P0 才可带」，不是「P0 必带」**（单条与整体两层，`validator.py:163-166`、`259-284`）：

```
[用例#1 TC-KB-020] @smoke 只能标在 P0 用例上（当前 priority=P1）；
@smoke 表示「部署后最先跑的最小集合」，是 P0 的子集而非全部 P0

冒烟集为空：存在 1 条 P0 用例，但没有任何 @smoke —— @smoke 表示「部署后最先跑的最小集合」，应当从 P0 中选取

冒烟集过大：26 条 @smoke 超过上限 6 条（= min(25, 20% × 30 条用例)）；
@smoke 是「最小可跑集合」，不是「全部 P0」——请只保留主干用例（每需求最多 1 条，排除边界与异常）
```

「冒烟集为空」只在**存在 P0 用例**时报错（无 P0 则不管）。

**实测：旧口径把冒烟集退化成「P0 的同义词」**（新占比为本次用 `select_smoke` 对同一批产物重算）：

| 产物 | 用例数 | P0 数 | 产物中 `@smoke` 数 | 占比 | 按现规则重选 | 重选占比 | 上限 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| v1（`c653dc92`，旧口径） | 94 | 26 | 26 | 27.7% | 12 | 12.8% | 18 |
| v2（`5b526ce6`，旧口径） | 121 | 90 | 90 | **74.4%** | 24 | **19.8%** | 24 |
| v3（`56b4686c`，旧口径） | 71 | 18 | 18 | **25.4%** | 7 | **9.9%** | 14 |
| 修复后（`7e84a0d3`，现行规则） | 80 | 45 | 16 | 20.0% | 16 | 20.0% | 16 |

重选结果 v2 24/121、v3 7/71 与文档口径的 19% / 9% 一致（差异仅来自四舍五入）；最后一行 `@smoke` 数 16
恰好等于上限 `min(25, 20%×80)=16`，说明限流生效。另有一次产物（`outputs/91719f0a…`，101 条）`@smoke` 高达
**85 条**而重选仅 20 条——该次标签未按规则统一，会被「冒烟集过大」拦截。

**审计字段的含义**（均由系统写入）：

| 字段 | 写入者 | 含义 |
| --- | --- | --- |
| `prioritySource` | `regrade` | 恒为 `"graded"`：优先级来自确定性重判，而非模型自评 |
| `priorityBasis` | `regrade` | `{requirement, dimension}`，解释「为什么是这个级别」 |
| `modelPriority` | `regrade` | 仅当模型原判 ≠ 重判结果时写入，用于对比 |
| `smokeReason` | `apply_smoke` | 仅冒烟集内用例：`{priority: "P0", dimension}`，说明入选依据 |

## 11. 用例状态枚举

`CASE_STATUSES`（`validator.py:26`，7 个）与追踪矩阵参与性：

| 状态 | 语义 | 参与追踪矩阵 |
| --- | --- | --- |
| `active` | 有效、纳入执行（落盘缺省值，`io.py:119`） | ✅ |
| `needs_review` | 待人工复核 | ✅ |
| `draft` / `reviewed` / `obsolete` | 旧值：草稿 / 已评审 / 已作废 | ✅ |
| `deprecated` / `superseded` | 已废弃 / 已被替代 | ❌ 被 `build_traceability` 跳过（`traceability.py:12`） |

`status` **本身不是必填**：只有字段存在且取值不在枚举内才报 `status 非法: xxx`（`validator.py:154-155`）。
矩阵只排除 `deprecated`/`superseded`，是「旧值兼容」的延伸——历史产物里的 `draft`/`reviewed`/`obsolete`
不应让需求变成「未覆盖」。

## 12. 重复判定（C-007）

去重签名 `case_signature`（`validator.py:59-71`）：
`sha256( normalize(module)#normalize(action)>normalize(target)|… #normalize(expected)|… )[:16]`，
`normalize(x)` = 去除所有空白 + 转小写。**刻意不含 `data`**：判定口径是「同模块 + 同步骤 + 同预期」，
同一操作序列换输入数据仍属「为凑数量的同质用例」。真实报错原文：

```
[用例#1] 与用例 TC-KB-019 重复（同模块 + 同步骤 + 同预期）；请删除其一，或改用不同的步骤/预期
```

签名登记表由调用方在**一次批量校验内共享**，因此查重是**跨文件、跨批次**的
（同模块用例可能分散在不同 JSON 里，`validator.py:216-218`）。

**方法论：同步骤签名 ≠ 重复。** 等价类与边界值本就「同流程、不同数据」，只有把 `expected` 也算进去的
三要素全同口径才能作为判重依据。本次用两种签名对真实产物做对照：

| 产物 | 用例数 | 步骤签名（module+steps）多成员组 | 涉及用例 | 三要素签名（含 expected）多成员组 |
| --- | ---: | ---: | ---: | ---: |
| v1 | 94 | 3 | 6 | 0 |
| v2 | 121 | **5** | **13** | 0 |
| v3 | 71 | **4** | **9** | 0 |

（与评测报告逐组人工核查的结论一致：v2 的 5 组/13 条、v3 的 4 组/9 条。）逐组核查后**全部是合法的
等价类/边界值变体**，例如 v3 的同一组：`TC-KB-006`（`docName=12345678901234567890`，断言恰好 20 字被接受、计数 20/20）
与 `TC-KB-007`（`docName=123456789012345678901`，断言第 21 字被禁止输入、计数稳定 20/20）。
教训：若把「步骤相同」当重复，会**误伤正确的测试设计**（把边界对砍掉一条）。跨批次重复另由管线侧
`dedup_cross_batch` 处理；按评测记录，v1 的 KB 模块曾因两批各自覆盖全部 16 条需求而产生约 **52%** 跨批重复，
v2/v3 已降至 0（该比例为评测结论，本次未逐批复算）。

## 13. 追踪矩阵

结构（`traceability.build_traceability`，`traceability.py:5-46`）：

| 键 | 类型 | 内容 |
| --- | --- | --- |
| `schemaVersion` | string | `"1.0"` |
| `requirements` | object[] | 每条需求一行：`{id, module, priority, caseIds[], status}` |
| `caseToRequirements` | object[] | `{caseId, requirementIds[]}`，按 `caseId` 排序 |
| `unknownReferences` | object[] | `{caseId, requirement}`：用例引用了**不存在的需求** |
| `duplicateMappings` | object[] | `{caseId, requirement}`：同一用例的 `requirement` 数组里**同一 REQ 重复出现** |
| `summary` | object | `{total, covered, insufficient, uncovered, excluded, coverageRate}` |

覆盖状态（`traceability.py:30`）：`excluded` = 需求对象带 `excluded: true`（`src/` 中只读不写，写入方未找到，
见附录 A）；`covered` = `caseIds` 数 ≥ `min_cases`；`insufficient` = `0 < caseIds 数 < min_cases`；
`uncovered` = `caseIds` 为空。`coverageRate = round((covered + insufficient) / eligible * 100, 1)`，
`eligible = total - excluded`（`traceability.py:45`）。`min_cases` 来自 profile，`generic`/`ecommerce` 均默认 `1`
（`profiles.py:5-10`），可由 `--min-cases-per-requirement` 覆盖。

校验规则 `validate_traceability`（`traceability.py:49-55`），真实报错原文（用构造矩阵触发）：

```
用例 TC-A-003 引用了不存在的需求 REQ-A-999
用例 TC-A-001 重复映射需求 REQ-A-001
P0 需求 REQ-A-002 没有 active 用例覆盖
```

第三条仅在 `fail_uncovered_p0=True`（默认）**且需求优先级为 P0** 时触发；`uncovered` 的非 P0 需求
只影响 `coverageRate`，不报错。pipeline 中该矩阵在 `regrade` 之后、落盘之前构建并校验（`pipeline.py:481-483`）。
实测 `outputs/7e84a0d3…` 的 `traceability.json`：`total=32, covered=32, insufficient=0, uncovered=0,
excluded=0, coverageRate=100.0`，`unknownReferences=0`、`duplicateMappings=0`。

## 14. 自然语言步骤渲染（导出用）

**为什么用确定性渲染而非让模型另写一套**：① `target` 里本就带中文元素名（`智能体配置页 (page=config)`），
动词映射即可成句，无需额外推理；② 不会与结构化步骤矛盾——结构化那份才是门禁与执行的依据，
若由模型另写一套，两份描述**一旦分叉就无法追责**；③ 零 token 成本、零非确定性，`--repro` 下逐字可复现。
实现见 `nlsteps.py`（`_VERBS` `nlsteps.py:33-46`、`_ASSERTIONS` `nlsteps.py:47-60`、`step_sentence` `nlsteps.py:89-111`）；
**断言类统一加「验证」前缀**，使评审稿一眼区分「操作」与「检查点」。

真实渲染样例（`TC-KB-020` 的 9 步，`render_steps_nl`）：

```
1. 打开「知识库配置」        2. 点击「启用开关」      3. 验证「启用开关」状态为「off」
4. 点击「命中测试按钮」      5. 等待「命中测试弹窗」   6. 在「测试问题输入框」输入「入住手续」
7. 点击「查询按钮」          8. 等待「召回结果」      9. 验证「目标文档片段」不可见
```

内联形态（XMind 窄节点用，`render_steps_nl_inline`）：`打开「知识库配置」；点击「启用开关」；验证「启用开关」状态为「off」；…；验证「目标文档片段」不可见。`
三个入口：`render_steps_nl`（带序号行列表）、`render_steps_nl_text`（单元格/段落）、`render_steps_nl_inline`（单行）。

| 边界行为（已实测） | 输出 | 规则 |
| --- | --- | --- |
| `fill` 且 `data` 为空 | `在「搜索框」输入` | 模板里的 `「{d}」` 被整体剥除，避免空引号（`nlsteps.py:96-99`） |
| `reload` 无 target | `刷新页面` | 动作模板本身不含 `{t}` |
| 未知动作 `assertThings` | `assertThings「x」：y` | 未知动作回退为「动作 + 元素」，**不丢信息**（`nlsteps.py:106-108`） |
| 历史动作 `expectHidden` | `验证「旧弹窗」不可见` | 渲染层保留该历史映射（`nlsteps.py:49`），但**它不在 23 词表内**，新产物不应出现 |

**`strip_locators` 只剥离带 `种类=` 的标注**：`_LOCATOR_IN_PROSE_RE`（`nlsteps.py:138-140`）只匹配
`data-testid|id|class|page|tab|text|name|label|placeholder` **后接 `=`** 的括号，因此**裸中文括号是正常补充说明、
不会被误删**：

| 输入 | 输出 |
| --- | --- |
| `已进入智能体配置页（page=config）` | `已进入智能体配置页` |
| `保留（id=docName）与说明` | `保留与说明` |
| `列表按名称排序（仅按名称）` / `提示文案（含标点，无定位符）` | 原样保留（裸括号） |
| `（page=config）`（剥离后为空） | **原样保留**：宁可保留原文，也不产出空字段（`nlsteps.py:158-159`） |

剥离后还会清掉被掏空的括号、合并重复空格、去掉尾部孤立的逗号/分号；**只影响导出渲染，JSON 原文保持不变**
（可追溯）。`clean_items` 对字符串数组逐项应用同一函数。

## 15. 完整示例与「会被门禁拒绝」的反例

### 15.1 合规示例（真实产物 `TC-KB-020`，逐条通过 `validate_case` + `validate_targets`）

```json
{
  "id": "TC-KB-020",
  "module": "KB",
  "subModule": "ENABLE_TOGGLE",
  "title": "关闭启用后命中测试不召回该文档片段",
  "design_method": "错误推测",
  "priority": "P0",
  "tags": ["@regression", "@kb"],
  "requirement": ["REQ-KB-009"],
  "preconditions": ["存在解析成功且默认开启的文档，文档片段命中关键词入住手续"],
  "testData": ["retrieveQuery=入住手续"],
  "steps": [
    { "action": "goto", "target": "知识库配置 (page=knowledge)", "data": "" },
    { "action": "click", "target": "启用开关 (class=toggle)", "data": "" },
    { "action": "expectState", "target": "启用开关 (class=toggle)", "data": "off" },
    { "action": "click", "target": "命中测试按钮 (class=primary)", "data": "" },
    { "action": "waitFor", "target": "命中测试弹窗 (class=modal)", "data": "" },
    { "action": "fill", "target": "测试问题输入框 (id=retrieveQuery)", "data": "入住手续" },
    { "action": "click", "target": "查询按钮 (class=primary)", "data": "" },
    { "action": "waitFor", "target": "召回结果 (class=result)", "data": "" },
    { "action": "expectNotVisible", "target": "目标文档片段 (class=chunk)", "data": "" }
  ],
  "expected": ["关闭启用开关后，命中测试召回结果区不包含该目标文档的任何 chunk 片段"],
  "prioritySource": "graded",
  "priorityBasis": { "requirement": "P0", "dimension": "反向" },
  "status": "active"
}
```

| 检查项 | 本例取值 |
| --- | --- |
| id / module | `TC-KB-020` 命中 `ID_RE`，前缀与 `module=KB` 一致 |
| design_method | `错误推测` ∈ 6 值 |
| tags | 非空且**无 `@smoke`**——该用例维度是「反向」，冒烟集只从正向主干里选（**P0 ≠ 冒烟集**） |
| requirement | `REQ-KB-009` 命中 `REQ_RE` 且属于 KB 模块真实需求 |
| steps / data | 9 步，动作全在 23 词表内，`data` 全为字符串 |
| 断言 | `expectState` + `expectNotVisible` 共 2 个（≥1）；`waitFor` 只作同步等待，不计入 |
| target | 种类与取值一致：`page=knowledge`，`class=toggle\|modal\|primary\|result\|chunk`，`id=retrieveQuery` |
| testData | `["retrieveQuery=入住手续"]`，值标量 |
| expected | 31 字，含具体断言对象（召回结果区 / chunk 片段），不命中空洞词表 |
| 审计字段 | `prioritySource`/`priorityBasis` 说明 P0 来自「P0 需求 × 反向维度」 |

另一个用到 `upload`/`expectText` 的真实用例 `TC-KB-017`（P1、正向）：`upload(class=file-drop, data=v2.txt)`、
`expectText(class=badge, data=解析中)`。

### 15.2 会被拒绝的反例（报错原文由当前代码实际输出）

| # | 反例（相对上例的改动） | 门禁报错原文 | 根因 |
| --- | --- | --- | --- |
| 1 | `priority` 改 P1 且保留 `@smoke` | `@smoke 只能标在 P0 用例上（当前 priority=P1）；@smoke 表示「部署后最先跑的最小集合」，是 P0 的子集而非全部 P0` | 冒烟集方向错误（§10） |
| 2 | 删掉全部 `expect*` 步骤 | `缺少断言步骤：用例必须至少包含一个断言动作（expectChecked/…/expectVisible）；仅用 click/fill/goto/waitFor 无法判定通过或失败` | 无判定依据（§5） |
| 3 | `steps[0].action` 改 `verify` | `step.action 非法: verify；可选 ['check','click',…,'waitFor']` | 词表外动作；报错带全量可选项 |
| 4 | `id` 改 `TC-kb-20` | `id 格式非法: TC-kb-20` | 违反 `ID_RE`（§2） |
| 5 | 只允许 `AUTH` 的批次里保留 `module=KB` | `module 非法: KB` | 模块集合由批次输入决定（§2） |
| 6 | 删除 `design_method` | `缺少 design_method（必须标注测试设计方法）` | C-005 |
| 7 | `design_method` 改 `因果图` | `design_method 非法: 因果图；可选值 ['等价类划分','边界值分析','判定表','状态迁移','场景法','错误推测']` | 枚举外取值 |
| 8 | `requirement` 改 `["KB-009"]` | `requirement 格式非法: KB-009` | 违反 `REQ_RE` |
| 9 | `expected` 改 `[]` | `expected 为空` | 非空约束 |
| 10 | `expected` 改 `["表现正常"]` | `预期不可验证（空洞断言）: '表现正常'；请改为可断言的表述（具体 UI 元素/数据/状态/金额）` | 空洞词表命中且长度 ≤12 |
| 11 | `expected` 改 `["跳转首页"]` | `预期过短且无具体值: '跳转首页'；请补充具体断言对象` | 长度 ≤8 且无数字（对比：`["共3条"]` 因含数字放行） |
| 12 | `testData` 改 `{"doc":{"name":"x"}}` | `testData.doc 是嵌套对象/数组，必须改为「键=值」标量（复杂数据请写进 preconditions）` | 嵌套会写坏 JSON（§6） |
| 13 | `testData` 改 `[{"a":1}]` | `testData[0] 是嵌套对象/数组，必须改为「键=值」字符串（复杂数据请写进 preconditions）` | 同上 |
| 14 | 某步 `data` 改 `{"a":1}` | `steps[6].data 必须是字符串/标量，不能是嵌套对象或数组` | 同上（`steps[].data`） |
| 15 | `tags` 改 `[]` | `tags 为空` | 非空约束 |
| 16 | 删除 `title` | `缺少 title` | 非空约束 |
| 17 | 同批再放一条签名相同的用例 | `与用例 TC-KB-019 重复（同模块 + 同步骤 + 同预期）；请删除其一，或改用不同的步骤/预期` | C-007（§12） |
| 18 | 存在 P0 但无任何 `@smoke` | `冒烟集为空：存在 1 条 P0 用例，但没有任何 @smoke —— …应当从 P0 中选取` | 整体层面约束（§10） |
| 19 | 30 条用例里标 26 条 `@smoke` | `冒烟集过大：26 条 @smoke 超过上限 6 条（= min(25, 20% × 30 条用例)）；…` | 冒烟集规模上限 |
| 20 | `target` 改 `工具页签 (id=tools)` | ``target 的 `id=tools` 不是契约中的真实 id（契约有 20 个：['app','chat',…]…）；请改用同种类的真实取值`` | 种类与契约不一致（§4） |
| 21 | 用例引用不存在的需求 | `用例 TC-A-003 引用了不存在的需求 REQ-A-999` | 追踪矩阵校验（§13） |

> 上表 1/2/3/6/7/10/11/12/13/14/15/16/17 由 `validate_case` + `check_duplicate` 输出，
> 18/19 由 `check_smoke_set` 输出，20 由 `prototype.validate_targets` 输出，21 由 `validate_traceability` 输出；
> 均为**当前代码的实际输出文本**（仅对超长可选项列表做了省略）。

## 附录 A：未复现 / 未验证项

| 项 | 状态 |
| --- | --- |
| `priority.py:20` 记录的「边界/异常一律 P2 → P0=1 / P2=50」 | ⚠ **未复现确切数字**（复算见 §9） |
| 「target 契约合规 ⇒ 可直接驱动 Playwright」 | ⚠ 未验证：合规率可以 100%，但不等于能跑通 |
| `subModule` 命名 | ⚠ 无枚举与命名门禁；本次复算缺失数：v1 0/94、v2 **29/121**、v3 **20/71**、修复后产物 0/80，取值风格也不统一 |
| `@regression` 全量约定 | ⚠ 仅约定（prompt/示例），`src/` 中无门禁；只有 stats/导出按标签统计 |
| `createdBy` | 历史字段，`src/` 零引用；是否保留由未来版本决定 |
| 需求级 `excluded` 字段的产出方 | ⚠ `traceability` 只读不写，`src/` 中未见写入点 |

## 附录 B：相关文档

- [prompts-and-gates.md](prompts-and-gates.md)：约束如何下发与回灌
- [quality-evaluation.md](quality-evaluation.md)：评测方法与最近一次真实产物评分
- [pipeline.md](pipeline.md)：三阶段流水线与产物目录
- [export.md](export.md)：Excel / XMind 导出的字段映射与 OOXML 细节
- [ingestion.md](ingestion.md)：输入分流、原型与图片处理
- [prompts-and-gates.md](prompts-and-gates.md)：门禁常量与提示词约束
