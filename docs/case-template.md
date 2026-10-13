# 用例字段规范（case-template.md）

测试用例的**机读格式**（JSON）。Markdown 评审稿由该 JSON 渲染，两者字段一致。

新产物顶层格式为 `{"schemaVersion":"2.0","actionSchemaVersion":"1.0","cases":[...]}`；动作集合按 `actionSchemaVersion` 维护。校验器和 02 执行覆盖工具仍兼容旧版顶层数组及没有动作版本字段的对象，无需迁移历史文件。

## 1. 用例 JSON 字段

| 字段 | 必填 | 类型 | 说明 / 枚举 |
| --- | --- | --- | --- |
| `id` | ✅ | string | `TC-<MOD>[-<SUB>]-<3位序号>`，全局唯一，如 `TC-AUTH-LOGIN-001` |
| `module` | ✅ | string | 模块代号（见下表） |
| `subModule` | - | string | 子模块，如 `LOGIN` |
| `title` | ✅ | string | 用例标题（一句话，动宾结构） |
| `design_method` | - | string | 采用的测试设计方法：`等价类划分`/`边界值分析`/`判定表`/`状态迁移`/`场景法`/`错误推测`（prompt 约束要求尽量标注） |
| `priority` | ✅ | string | `P0`/`P1`/`P2`/`P3`（含义见 conventions） |
| `tags` | ✅ | string[] | `@smoke`/`@regression`/`@auth`/`@cart`/…，P0 必含 `@smoke` |
| `requirement` | ✅ | string | 回溯锚点，`REQ-<MOD>-<SEQ>`（可多个，用数组） |
| `preconditions` | - | string[] | 前置条件，如「已存在注册用户」「已登录」 |
| `testData` | - | string[] | 测试数据，**「键=值」字符串数组**：`["phone=13800138000","password=demo123456"]`。值只能是标量（数字/布尔/短文本）；**禁止嵌套对象或数组**、禁止值内出现 `"` 或 `\`、禁止真实换行（用「；」分隔）。复杂数据写进 `preconditions` |
| `steps` | ✅ | array | 步骤数组，每步见下 |
| `expected` | ✅ | string[] | 预期结果（可断言点，≥1） |
| `createdBy` | - | string | `ai`/`human` |
| `status` | - | string | `active`/`needs_review`/`deprecated`/`superseded`；旧值 `draft`/`reviewed`/`obsolete` 读取时兼容 |

### steps[] 每步字段

| 字段 | 必填 | 说明 / 枚举 |
| --- | --- | --- |
| `action` | ✅ | 取自 `prd2tc.actions` 的**单一事实源**（`ACTION_NAMES`，23 个）：操作类 `goto`/`fill`/`click`/`selectOption`/`check`/`uncheck`/`upload`/`hover`/`press`/`reload`/`scrollTo`/`waitFor`；断言类 `expectVisible`/`expectNotVisible`/`expectText`/`expectValue`/`expectEnabled`/`expectDisabled`/`expectChecked`/`expectUnchecked`/`expectState`/`expectCount`/`expectUrl`。**每条用例至少 1 个断言** |
| `target` | ✅ | 定位描述（页面/元素名，如「登录按钮」「/login」） |
| `data` | - | 输入数据或期望值（配合 expect 类动作）。**必须是字符串**，不能是嵌套对象/数组 |

> `action` 与 `target` 对齐 Playwright 概念，便于后续生成 spec 骨架。

## 2. 模块代号（与 conventions 一致）

`AUTH`（账号）、`HOME`（首页）、`PROD`（商品）、`SEARCH`（搜索筛选）、`CART`（购物车）、`CHECKOUT`（结算）、`COUPON`（优惠券）、`ORDER`（订单）、`ACCOUNT`（个人中心）、`COMMENT`（评价）、`COMMON`（公共/兜底）、`PERF`（性能/兼容）。

## 3. 示例

```json
{
  "id": "TC-AUTH-LOGIN-001",
  "module": "AUTH",
  "subModule": "LOGIN",
  "title": "正确凭据登录成功并跳转首页",
  "priority": "P0",
  "tags": ["@smoke", "@auth"],
  "requirement": ["REQ-AUTH-002"],
  "preconditions": ["系统中存在 normal 状态账号"],
  "testData": ["phone=13800138000", "password=demo123456"],
  "steps": [
    { "action": "goto", "target": "/login", "data": "" },
    { "action": "fill", "target": "手机号输入框", "data": "13800138000" },
    { "action": "fill", "target": "密码输入框", "data": "demo123456" },
    { "action": "click", "target": "登录按钮", "data": "" },
    { "action": "expectUrl", "target": "/", "data": "" },
    { "action": "expectVisible", "target": "导航栏用户昵称", "data": "" }
  ],
  "expected": [
    "登录成功后跳转首页",
    "导航栏显示当前用户昵称",
    "购物车入口可用"
  ],
  "createdBy": "ai",
  "status": "draft"
}
```

## 4. 校验规则（orchestrate.mjs validate 强制执行）

1. `id` 必填且全局唯一，格式 `^TC-[A-Z]+(-[A-Z0-9]+)?-\d{3}$`。
2. `module` 必填且 ∈ 模块代号表。
3. `priority` ∈ {P0,P1,P2,P3}。
4. `tags` 非空数组；P0 必须含 `@smoke`。
5. `requirement` 非空数组，每项匹配 `^REQ-[A-Z]+-\d{3}$`。
6. `steps` 非空数组，每步 `action` ∈ 枚举、`target` 非空、`data` 为字符串。
7. `expected` 非空数组。
8. `testData` 若存在：容器可为「键=值」字符串数组（推荐）或旧版 object；但**值不得为嵌套对象/数组**（嵌套需要多层转义，会把 JSON 写坏）。旧版 object 在读取时自动归一化为字符串数组。

## 5. Markdown 评审稿渲染规则

- 按模块分节（`## <模块代号> <模块名>`）。
- 每条用例输出为表格行或代码块 JSON + 可读摘要，字段与 JSON 一致。
- 顶部附 traceability 表（REQ ↔ TC 覆盖情况）。

## 6. 导出格式（Excel / XMind）

用例 JSON 是权威机读源；同时自动导出两种可分发格式（`prd2tc export` 可单独重跑）：

| 格式 | 内容 | 字段映射 |
| --- | --- | --- |
| `.xlsx` | `用例明细`（13 列，P0/P1 条件着色、冻结表头、自动筛选）、`需求覆盖`（覆盖不足/未覆盖标红）、`统计概览` | 列顺序见 `docs/export-excel-xmind.md` §5.2 |
| `.xmind` | 模块 → 子模块 → 用例 三级导图 | 优先级 → `markers` 图标；`tags`/`design_method` → `labels` 文本标签；其余详情 → 节点 `notes` |

导出**不改动机读源**：两种格式都从 `cases/cases/*.json` 派生。`status` 与 `createdBy` 不导出。
完整设计（OOXML 子集、样式表、可复现性要求）见 [export-excel-xmind.md](export-excel-xmind.md)。
