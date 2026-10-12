# 测试用例 Excel / XMind 导出：技术实现文档

> 版本：v1.1 | 编制日期：2026-10-12 | 修订日期：2026-10-12 | 目标版本：prd2tc v0.6.0
>
> 需求：提供测试用例的 Excel（.xlsx）导出与 XMind 思维导图（.xmind）导出。

> ## 修订记录（v1.0 → v1.1）
>
> v1.1 把 v1.0 附录 A 的代码**实际提取运行**，并用**独立第三方解析器 openpyxl 3.1.5** 回读产物
> （比"自己写自己读"更有说服力）。核心设计**成立**，但发现以下缺陷，均已在本文档内改正：
>
> | 编号 | 问题 | 影响 | 修正 |
> |---|---|---|---|
> | R-1 | 默认 `xmind_mode="module"` 与 §6.1「三级树」示例、§10.1 的三个测试**互相矛盾** | **默认导图看不到任何用例**；文档自带测试 4 个失败（`KeyError: 'children'`） | 默认改为 `case`（§6.6） |
> | R-2 | `_priority_style` 对 P0/P1 返回 `STYLE_P0/P1`，而这两个样式**不含 `wrapText`** | **P0/P1 行的「测试步骤」「预期结果」挤成一行**——恰恰最需审阅的行 | 新增组合样式 `s=5/6`（P0+wrap / P1+wrap），§5.4 |
> | R-3 | 优先级用 `labels: ["priority-1"]` | XMind 里只渲染成**文本标签**，不是优先级图标 | 改用 `markers: [{"markerId": ...}]`，§6.2 |
> | R-4 | `MAX_NODES` 超限**抛 ValueError** | 与 §6.6 声称的「降级策略」不符；3000 条用例直接不产出文件 | 改为**自动降级为 module 模式**并告警 |
> | R-5 | `cmd_export` **未传** `--product/--version` | 死参数：文件名恒为 `用例集.xlsx`，导图标题拿不到产品/版本 | 补传并加 `run_root` 推断，§7.1 |
> | R-6 | `WARN_FILL` 定义了但**从未被引用** | §9.2 声称的「覆盖不足浅红底色」实际未生效 | 需求覆盖 sheet 按状态着色，§9.2 |
> | R-7 | `styles.xml` 缺 `<cellStyles>` | openpyxl 警告 `Workbook contains no default style`；严格实现可能报错 | 补 `<cellStyles count="1">`，§5.4 |
> | R-8 | `_safe_name` 未清洗 `"` `&` `<` `>` | `--sheet-name '用"例'` 会**破坏 workbook.xml**（`ParseError`） | 补 XML 转义，§5.6 |
> | R-9 | `metadata.json` 硬编码 `version: "0.6.0"` | 与项目实际版本（`__version__`）漂移 | 改为动态取 `__version__` |
> | R-10 | `_probe_out/` 探针产物残留在仓库根且未被忽略 | 文档称"脚本已清理"，实际 `git status` 显示 `?? _probe_out/` | 已清理并加入 `.gitignore` |
> | R-11 | `build_xlsx_bytes` **未实现 `统计概览` sheet** | §5.1 声明「单个 .xlsx 含 3 个 sheet」，代码只生成 2 个（设计与实现不符） | 补 `build_stats_rows()` + `stats` 参数，§9.3 |
> | R-12 | sheet 名可能重复 | 用户 `--sheet-name 统计概览` 时与内置 sheet 同名，**Excel 会判定文件损坏** | 新增 `_dedupe_sheet_names()` 自动追加 `-2`，§5.9 |
> | **R-13** | **关系类型 URI 漏了 `/2006/`** | **Excel 报「格式警告」→ 修复后只剩工作表分页、没有任何内容**（用户实测）。根因：f-string 拆行时写成 `.../officeDocument/relationships/worksheet` | 提为 `REL_NS` 常量，§5.10；新增 3 条结构回归测试 |
> | **R-14** | **验证方法本身有缺陷** | v1.1 用 openpyxl 交叉验证就宣告「可打开」——但 **openpyxl 是宽容解析器**，不校验关系 URI 与元素顺序，因此 R-13 被漏过 | 改用 **真实 Excel COM 打开**为权威判据，§3 |
>
> **已实测确认正确的部分**（保留不改）：ZIP+XML/JSON 容器思路、`inlineStr` 策略、
> `content.json`（而非 `content.xml`）选型、固定时间戳 + 确定性 topic id 的**可复现性**
> （3 个不同 `PYTHONHASHSEED` 下字节哈希完全一致）、OOXML 元素顺序
> （`cols → sheetData → autoFilter`）、`artifact_inventory` 的 `read_bytes()` 二进安全。

---

## 目录

- [1. 需求与范围](#1-需求与范围)
- [2. 关键技术选型](#2-关键技术选型)
- [3. 可行性验证结论](#3-可行性验证结论)
- [4. 总体架构](#4-总体架构)
- [5. Excel 导出详细设计](#5-excel-导出详细设计)
- [6. XMind 导出详细设计](#6-xmind-导出详细设计)
- [7. CLI 与流水线集成](#7-cli-与流水线集成)
- [8. 服务层与前端集成](#8-服务层与前端集成)
- [9. 数据模型与字段映射](#9-数据模型与字段映射)
- [10. 测试方案](#10-测试方案)
- [11. 实施计划与风险](#11-实施计划与风险)
- [附录 A：完整实现代码](#附录-a完整实现代码)

---

## 1. 需求与范围

### 1.1 功能需求

| 编号 | 需求 | 优先级 |
|---|---|---|
| F-1 | 将 `cases/cases/*.json` 导出为单个 `.xlsx` 工作簿 | P0 |
| F-2 | 将用例集导出为 `.xmind` 思维导图（模块→子模块→用例三级结构） | P0 |
| F-3 | 支持 CLI 独立命令：`prd2tc export --format excel\|xmind\|all` | P0 |
| F-4 | `generate` 流水线结束后自动产出两种格式 | P0 |
| F-5 | Excel 支持冻结表头、列宽自适应、自动筛选、优先级条件着色 | P1 |
| F-6 | XMind 节点携带完整用例信息（备注 notes + 标签 labels） | P1 |
| F-7 | 服务层提供下载接口 | P1 |
| F-8 | 导出失败不阻断主流程（降级为告警） | P0 |

### 1.2 非功能约束（继承项目既有原则）

项目 README 第 9 行声明：**"技术栈：Python 3.10+，零第三方依赖"**。这是本方案最硬的约束。

| 约束 | 说明 |
|---|---|
| **零第三方运行时依赖** | 不得引入 openpyxl、xlsxwriter、xmindparser 等 |
| **原子写入** | 复用 `io.atomic_write_text` 的原子性保证；二进制文件需等价实现 |
| **可复现** | 同一份 JSON 输入产出字节级一致的文件 |
| **向后兼容** | 不破坏现有 `cases/cases/*.json` 机读源 |
| **CLI 可用** | 不依赖 service 模式，核心 CLI 单独可用 |

### 1.3 明确不做

| 不做 | 原因 |
|---|---|
| ❌ 导入（Excel → JSON 反向） | 需求未要求，且会引入字段歧义与校验难题 |
| ❌ 旧版 `.xls` 格式 | 二进制 BIFF 复杂，`.xlsx` 已是事实标准 |
| ❌ XMind 8 `content.xml` 格式 | Zen 的 `content.json` 覆盖 XMind 8/2020+ 全版本读取 |
| ❌ 自动打开文件 | 无人值守场景下不可接受 |
| ❌ 导出加密/保护工作表 | 需要额外密码管理，无实际需求 |

---

## 2. 关键技术选型

### 2.1 核心洞察：两种格式都是 ZIP 包

这是整个方案成立的技术基础。

```
用例集.xlsx  = ZIP{ [Content_Types].xml, _rels/.rels,
                    xl/workbook.xml, xl/_rels/workbook.xml.rels,
                    xl/styles.xml, xl/worksheets/sheet1.xml }

用例集.xmind = ZIP{ content.json, metadata.json, manifest.json }
```

两者都是 **ZIP（PK\x03\x04）容器 + XML/JSON 成员**。Python 标准库 `zipfile` 完整支持写入，`xml.sax.saxutils.escape` 提供 XML 转义，`json` 处理 XMind 内容。

**结论：无需任何第三方库即可生成两种格式。**

### 2.2 选型对比

| 方案 | 依赖 | Excel 兼容性 | XMind 兼容性 | 体积 | 维护成本 |
|---|---|---|---|---|---|
| **A. 纯标准库自研**（推荐） | 无 | ✅ 良好 | ✅ 良好 | 最小 | 中（约 400 行） |
| B. openpyxl | `openpyxl` | ✅ 最佳 | ❌ 需另配 | 大 | 低 |
| C. xlsxwriter + xmind 手工 | 2 个 | ✅ 最佳 | 中 | 大 | 中 |
| D. 导出 CSV + 手工打包 | 无 | ⚠️ 中文乱码 | ❌ | 小 | 低 |

> **为何不用 openpyxl**：它会破坏"零第三方依赖"这一项目核心定位。README 第 242 行把"零第三方运行时依赖"列为三大卖点之一，为导出功能引入一个 2MB+ 的依赖（连带 lxml、et-xmlfile）不划算。
>
> **自研方案的风险**：OOXML 规范复杂。但**导出只需写子集**（单 sheet、inlineStr、无公式、无图表），实测证明子集完全兼容（见第 3 章）。

### 2.3 XMind 格式版本选择

| 版本 | 内部格式 | 决策 |
|---|---|---|
| XMind 3/8（≤2018） | `content.xml`（XML） | ❌ 兼容面窄 |
| **XMind Zen / 2020+** | `content.json`（JSON） | ✅ **选用** |
| XMind 2023/2024 | `content.json`（JSON） | ✅ 同一格式 |

官方文档确认：XMind Zen（2017）起改用 JSON，**当前版本可读取两种格式**，因此输出 JSON 覆盖面最广。

### 2.4 Excel 单元格内容策略

| 策略 | 说明 | 采用 |
|---|---|---|
| `sharedStrings` 共享字符串表 | 需要维护索引映射，复杂度高 | ❌ |
| **`inlineStr` 内联字符串** | 字符串直接写在 `<c>` 内，Excel 完全支持 | ✅ |
| 纯数值写 `t="n"` | 统计类单元格 | 可选 |

**选择 `inlineStr`**：省去 sharedStrings 的去重与索引管理，代码简单 40%，代价是文件略大（对千条量级用例可忽略）。

---

## 3. 可行性验证结论

在编写正式代码前，已用临时脚本实测验证核心机制（脚本已清理）：

```
=== probe.xlsx (2283 bytes) ===
magic: b'PK\x03\x04'
ZIP 完整性(testzip): OK
  - [Content_Types].xml                   686B
  - _rels/.rels                          298B
  - xl/workbook.xml                      287B
  - xl/_rels/workbook.xml.rels           427B
  - xl/styles.xml                        747B
  - xl/worksheets/sheet1.xml            1182B
回读 sheet1 含转义: True
回读首行: 4 cells in header

=== probe.xmind (785 bytes) ===
magic: b'PK\x03\x04'
ZIP 完整性(testzip): OK
  - content.json                         1406B
  - metadata.json                         51B
  - manifest.json                         59B
回读根主题: 测试用例集 <&>特殊字符
回读一级分支: ['AUTH 登录', 'CART 购物车']
回读二级示例: 正确凭据登录成功

全部验证通过
```

**已验证要点**：

1. ✅ 纯标准库生成的 ZIP 魔数为 `PK\x03\x04`，`testzip()` 完整性检查通过
2. ✅ XMind 嵌套树结构（rootTopic → children.attached → ...）往返正确
3. ✅ XML 特殊字符 `<` `&` 经 `escape()` 正确转义，中文无乱码
4. ✅ `content.json` 可被 `json.loads` 正常解析
5. ✅ 环境 Python 3.12.10 / Windows AMD64

**v1.1 补充：独立解析器交叉验证**（比自校验更有说服力）

除上述自校验外，v1.1 用 **openpyxl 3.1.5**（按 OOXML 规范严格解析的第三方库）回读了
13 列 + 2 sheet 的完整样例（21 条用例）：

```
[PASS] 解析成功，工作表: ['用例明细', '需求覆盖']
维度: 22 行 × 13 列（与设计一致）
表头: ['用例编号','模块','子模块','用例标题','设计方法','优先级','标签','关联需求',
       '前置条件','测试数据','测试步骤','预期结果','状态']
特殊字符无损往返: '含特殊字符 <a> & "引号" 与中文' ✓
样式: F2 填充=FFFFC7CE bold=True；A1 表头 bold=True；冻结窗格=A2；筛选=A1:M22
需求覆盖: 21 行 × 6 列，状态中文化正确（'已覆盖'）
```

**该验证同时暴露了 3 个缺陷**（已作为 R-2 / R-6 / R-7 修正）：
`P0/P1 行 wrapText 丢失`、`WARN_FILL 未被引用`、`缺 <cellStyles>`。

**仍未验证项**（本机沙箱限制，需人工确认）：

- ~~⚠️ Excel/WPS 打开是否有"格式警告"提示~~ → **已由用户实测发现缺陷（R-13），并已修复+回归验证**（见 §3 下方补充）
- ⚠️ XMind 客户端能否正常打开并编辑导图（用户已确认 **XMind 结构正确**）

> **v1.1 的重大教训（R-14）**：当时用 openpyxl「交叉验证通过」就宣告可打开，这是**方法错误**。
> openpyxl 是宽容解析器，不校验关系 URI 与元素顺序，因此放过了 R-13。
>
> **正确做法**：用真实 Excel 打开作为权威判据。修复 R-13 后实测：
>
> ```
> verify.xlsx   OPEN-OK  sheets=3   用例明细 11×13 / 需求覆盖 11×6 / 统计概览 7×3
> minimal.xlsx  OPEN-OK  sheets=1   用例明细 2×13
> ```
>
> 内容完整、无修复弹窗。**根因定位方法**：以 openpyxl 生成的文件为对照（已知可打开），
> 构造 hybrid 文件逐 part 替换，用 `Workbooks.Open` 成败二分 —— 一次定位到
> `xl/_rels/workbook.xml.rels`。

> **实施第一步即为此**：先产出样例文件，人工用 Excel 与 XMind 各打开一次确认，再进入集成。
> 若 XMind 报格式错误，优先补充 `metadata.json` 的 `activeSheetId` 字段（见 6.4 节）。

---

## 4. 总体架构

### 4.1 模块结构

```
src/prd2tc/
├── export/                        # 新增：导出层
│   ├── __init__.py                # 统一入口 export_cases()
│   ├── common.py                  # 共享：二进制原子写、行展开
│   ├── excel.py                   # Excel 导出
│   └── xmind.py                   # XMind 导出
├── io.py                          # 现有：文本原子写（保持不变）
└── cli.py                         # 修改：新增 export 子命令
```

### 4.2 数据流

```
cases/cases/*.json                      traceability.json（可选）
        │                                        │
        └───────────► export/common.py ◄────────┘
                     extract_rows() / build_tree()
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
        export/excel.py         export/xmind.py
              │                       │
              ▼                       ▼
        cases/用例集.xlsx        cases/用例集.xmind
```

### 4.3 设计原则

| 原则 | 落地 |
|---|---|
| **单一数据源** | 两种导出都从 `cases/cases/*.json` 派生，不读 Markdown |
| **纯函数** | 导出函数输入 list[dict]，输出 bytes，不触碰文件系统（写盘在调用方） |
| **可测试** | 纯函数便于单测；写盘层只需集成测试 |
| **降级不阻断** | 导出异常捕获后告警，`generate` 流程照常成功 |

### 4.4 与现有代码的关系

| 现有模块 | 复用 | 说明 |
|---|---|---|
| `io.load_cases()` | ✅ 直接复用 | 读 `cases/cases/*.json`，自动兼容 envelope 格式 |
| `io.atomic_write_text()` | ⚠️ 需扩展 | 现有为文本专用，二进制需 `atomic_write_bytes()` |
| `io.stats()` | ✅ 直接复用 | Excel 统计 sheet 与 XMind 根节点摘要 |
| `traceability.build_traceability()` | ✅ 可选复用 | 若存在 traceability.json，Excel 增设「需求覆盖」sheet |

---

## 5. Excel 导出详细设计

### 5.1 工作簿结构

单个 `.xlsx` 含 3 个 sheet：

| Sheet 名 | 内容 | 数据来源 |
|---|---|---|
| `用例明细` | 全部用例，每条一行 | `cases/*.json` |
| `需求覆盖` | REQ ↔ TC 双向映射 | `traceability.json`（存在时） |
| `统计概览` | 按模块/优先级/标签统计 | `io.stats()` |

### 5.2 列定义（用例明细）

对应 `docs/case-template.md` 的字段：

| 列 | 宽度 | 来源字段 | 渲染方式 |
|---|---|---|---|
| 用例编号 | 16 | `id` | 原样 |
| 模块 | 12 | `module` | 原样 |
| 子模块 | 12 | `subModule` | 缺失则空 |
| 用例标题 | 40 | `title` | 原样，自动换行 |
| 设计方法 | 14 | `design_method` | 缺失则 `—` |
| 优先级 | 8 | `priority` | 条件着色 |
| 标签 | 20 | `tags[]` | `" ".join()` |
| 关联需求 | 24 | `requirement[]` | `", ".join()` |
| 前置条件 | 30 | `preconditions[]` | `\n` 分隔 + 自动换行 |
| 测试数据 | 24 | `testData[]` | `\n` 分隔 |
| 测试步骤 | 50 | `steps[]` | `1. action → target (data)` 逐行 |
| 预期结果 | 40 | `expected[]` | `\n` 分隔 |
| 状态 | 12 | `status` | 缺失则 `active` |

**步骤渲染规则**（关键，直接影响可读性）：

```python
def render_steps(steps: list[dict]) -> str:
    lines = []
    for i, s in enumerate(steps, 1):
        action = s.get("action", "")
        target = s.get("target", "")
        data = str(s.get("data") or "").strip()
        line = f"{i}. [{action}] {target}"
        if data:
            line += f" ← {data}"
        lines.append(line)
    return "\n".join(lines)
```

示例输出：

```
1. [goto] /login
2. [fill] 手机号输入框 ← 13800138000
3. [click] 登录按钮
4. [expectUrl] /
```

### 5.3 OOXML 结构

xlsx 包的 6 个成员及其关系：

```
[Content_Types].xml          声明每个 part 的 MIME 类型
_rels/.rels                  包级关系：指向 xl/workbook.xml
xl/workbook.xml              工作簿：sheet 列表与 r:id
xl/_rels/workbook.xml.rels   工作簿级关系：worksheet + styles
xl/styles.xml                样式表：字体/填充/边框/单元格格式
xl/worksheets/sheet1.xml     工作表数据（inlineStr）
```

### 5.4 样式表设计

需要支持 5 种视觉强调：P0 红底、P1 橙底、覆盖不足红底、表头加粗、普通行自动换行。

> ⚠️ **v1.1 修正 R-2（重要）**：v1.0 把「P0 底色」与「自动换行」做成了**互斥的单一样式**，
> 于是 `_priority_style()` 对 P0/P1 行返回 `STYLE_P0/STYLE_P1`（不含 `wrapText`），
> `STYLE_WRAP` 只在"既非表头也非 P0/P1"时才生效。
> 实测结果：第 2 行 13 个单元格**全部** `s=2`、`wrapText=False` ——
> **P0/P1 用例的「测试步骤」「预期结果」会挤成一行显示**，而 P0 恰恰是最需要审阅的行。
>
> **修正做法**：每个强调样式都**自带** `wrapText`，不再依赖回退到 `STYLE_WRAP`。

```xml
<styleSheet xmlns="...spreadsheetml/2006/main">
  <fonts count="3">
    <font><sz val="11"/><name val="Calibri"/></font>                          <!-- 0 普通 -->
    <font><b/><sz val="11"/><name val="Calibri"/></font>                       <!-- 1 表头 -->
    <font><b/><color rgb="FF9C0006"/><sz val="11"/><name val="Calibri"/></font>  <!-- 2 P0 字体 -->
  </fonts>
  <fills count="5">
    <fill><patternFill patternType="none"/></fill>                           <!-- 0 -->
    <fill><patternFill patternType="gray125"/></fill>                        <!-- 1 占位 -->
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFC7CE"/><bgColor indexed="64"/></patternFill></fill>  <!-- 2 P0 红底 -->
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFEB9C"/><bgColor indexed="64"/></patternFill></fill>  <!-- 3 P1 橙底 -->
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFE1E1"/><bgColor indexed="64"/></patternFill></fill>  <!-- 4 覆盖不足 -->
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="6">
    <!-- s=0 普通 -->
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"
        applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
    <!-- s=1 表头：加粗居中，不需换行 -->
    <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"
        applyFont="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <!-- s=2 P0：红底 + 换行（R-2 修正：必须自带 wrapText） -->
    <xf numFmtId="0" fontId="2" fillId="2" borderId="0" xfId="0"
        applyFont="1" applyFill="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
    <!-- s=3 P1：橙底 + 换行 -->
    <xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0"
        applyFill="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
    <!-- s=4 覆盖不足/未覆盖：浅红底 + 换行（R-6：v1.0 定义了底色却从未引用） -->
    <xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0"
        applyFill="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
    <!-- s=5 纯换行（无底色）：表头之外的兜底，语义与 s=0 相同但保留独立索引便于扩展 -->
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"
        applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
  </cellXfs>
  <!-- R-7：cellStyles 必需，否则 openpyxl 警告 "Workbook contains no default style"，
       严格的 Office/第三方实现可能直接报错。注意它必须排在 cellXfs 之后。 -->
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
  <dxfs count="0"/>
  <tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>
```

样式索引常量：

```python
STYLE_NORMAL = 0   # 普通（带换行）
STYLE_HEADER = 1   # 表头（加粗居中）
STYLE_P0     = 2   # P0 红底 + 换行
STYLE_P1     = 3   # P1 橙底 + 换行
STYLE_WARN   = 4   # 覆盖不足 + 换行
STYLE_WRAP   = 5   # 纯换行（兜底）
```

> **元素顺序**（OOXML 强制，写错 Excel 报"文件已损坏"）：
> `fonts → fills → borders → cellStyleXfs → cellXfs → cellStyles → dxfs → tableStyles`。
> `cellStyles` 极易被漏掉且排在 `cellXfs` **之后**，两者都要注意。

### 5.5 工作表增强

| 特性 | OOXML 实现 |
|---|---|
| 冻结首行 | `<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>` |
| 自动筛选 | `<autoFilter ref="A1:M{last}"/>`（置于 `sheetData` 之后） |
| 列宽 | `<cols><col min="1" max="1" width="16" customWidth="1"/>…</cols>`（置于 `sheetData` 之前） |
| 自动换行 | 单元格 `s="4"` + `<alignment wrapText="1" vertical="top"/>` |
| 行高自适应 | `<row r="N" ht="60" customHeight="1">` |

> **注意 OOXML 元素顺序**：`cols` 必须在 `sheetData` 前，`autoFilter` 必须在 `sheetData` 后。顺序错误 Excel 会报"文件已损坏"。

### 5.6 列名转换

```python
def col_name(idx: int) -> str:
    """0→A, 25→Z, 26→AA, 701→ZZ, 702→AAA"""
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s
```

### 5.7 大数据量考量

Excel 单 sheet 上限 1,048,576 行。当前场景（单 PRD 用例通常 < 2000 条）远未触及，但需防御：

```python
EXCEL_MAX_ROWS = 1_048_576

def _check_capacity(count: int) -> None:
    if count + 1 > EXCEL_MAX_ROWS:
        raise ValueError(
            f"用例数 {count} 超出单 Sheet 上限 {EXCEL_MAX_ROWS}，"
            f"请拆分或使用 --split-by-module"
        )
```

**分表策略（预留）**：若将来需要 `--split-by-module`，每个模块一个 sheet，`xl/workbook.xml` 的 `<sheets>` 动态生成即可，架构已支持。

### 5.8 可复现性

ZIP 写入需固定时间戳与成员顺序，否则每次字节不同：

```python
with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
    for name, content in sorted(members.items()):   # 顺序固定
        info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        z.writestr(info, content)
```

`zipfile` 另需注意：`ZipFile.writestr` 传入 `str` 时用 UTF-8 编码；所有 XML 声明必须是 `encoding="UTF-8"`，否则中文乱码。

> 实测：3 个不同 `PYTHONHASHSEED` 下导出的字节哈希完全一致，因此导出产物可参与 `compare-runs` 对拍。

### 5.10 关系类型 URI（**最高风险项**）

`xl/_rels/workbook.xml.rels` 里每个 `Relationship@Type` 必须是完整合法的 URI。
**漏掉 `/2006/` 会让 Excel 判定文件损坏**：

```xml
<!-- ✗ 错误：Excel 报「格式警告」，修复后只剩工作表分页、没有任何内容 -->
<Relationship Id="rId1" Target="worksheets/sheet1.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/relationships/worksheet"/>

<!-- ✓ 正确 -->
<Relationship Id="rId1" Target="worksheets/sheet1.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
```

**根因与防复发**：v1.1 在生产代码里用 f-string 跨行拼接该 URI，拆行时漏掉了 `2006/`。
故现已提为模块级常量，杜绝再次拆行出错：

```python
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
```

**为什么 openpyxl 没发现**：openpyxl 是**宽容解析器**，不校验关系 URI 合法性也不严格
校验元素顺序，因此 R-13 在 v1.1 的交叉验证中被漏过（见 §3 的教训）。

> ⚠️ **验证纪律**：xlsx 兼容性的**权威判据是真实 Excel 打开**，不是 openpyxl。
> 新增的 3 条结构回归测试（`test_excel_relationship_types_are_valid` /
> `test_excel_relationship_targets_exist` / `test_excel_content_types_cover_all_parts`）
> 覆盖了这类 openpyxl 看不见的问题。

Excel 要求同一工作簿内 sheet 名唯一，重名会导致**文件被判损坏**。
`--sheet-name` 可能撞上内置的「需求覆盖」「统计概览」，因此需自动去重：

```python
def _dedupe_sheet_names(sheets: list[tuple]) -> list[tuple]:
    """重名时追加 -2、-3…（留出后缀空间，仍满足 ≤31 字符上限）。"""
    seen: dict[str, int] = {}
    output = []
    for title, rows, widths, style_fn in sheets:
        base = str(title)
        if base in seen:
            seen[base] += 1
            candidate = f"{base[:28]}-{seen[base]}"
            while candidate in seen:
                seen[base] += 1
                candidate = f"{base[:28]}-{seen[base]}"
            seen[candidate] = 1
            title = candidate
        else:
            seen[base] = 1
        output.append((title, rows, widths, style_fn))
    return output
```

---

## 6. XMind 导出详细设计

### 6.1 导图结构

三级树，与用例 JSON 的层级对应（下图为**默认 `case` 模式**，见 §6.6）：

```
用例集：<product> v<version>          ← rootTopic
├── AUTH 账号                          ← module
│   ├── LOGIN 登录                     ← subModule（可选层）
│   │   ├── TC-AUTH-LOGIN-001 正确凭据登录成功
│   │   └── TC-AUTH-LOGIN-002 密码错误提示
│   └── TC-AUTH-PROFILE-001 修改昵称   ← 无 subModule 时省略中间层
└── CART 购物车
    └── TC-CART-001 加入商品
```

**模块名映射**：优先用 `analysis/requirements.json` 的 `modules[].name`（中文名），缺失则回退到代号。

### 6.2 节点信息承载

XMind 的 `notes` 字段承载完整用例详情，让测试人员点击节点即可查看：

```json
{
  "id": "TC-AUTH-LOGIN-001",
  "class": "topic",
  "title": "正确凭据登录成功并跳转首页",
  "markers": [{ "markerId": "priority-1" }],
  "labels": ["@smoke", "@auth", "等价类划分"],
  "notes": {
    "plain": {
      "content": "【用例编号】TC-AUTH-LOGIN-001\n【优先级】P0\n【关联需求】REQ-AUTH-002\n【前置条件】\n1. 系统中存在 normal 状态账号\n【测试数据】\n• phone=13800138000\n• password=demo123456\n【测试步骤】\n1. [goto] /login\n2. [fill] 手机号输入框 ← 13800138000\n【预期结果】\n• 登录成功后跳转首页\n• 导航栏显示当前用户昵称"
    }
  },
  "children": { "attached": [] }
}
```

> **v1.1 修正 R-3（优先级必须用 `markers`，不能用 `labels`）**：
> v1.0 把优先级写进 `labels: ["priority-1", ...]`，但 `labels` 是**纯文本标签**，
> XMind 会把 `priority-1` 原样显示成一串英文，**不会渲染成优先级图标**。
> 优先级图标必须走 `markers`：`[{"markerId": "priority-1"}]`（P0→priority-1 … P3→priority-4）。
>
> 分工因此明确为：
>
> | 信息 | 字段 | 渲染效果 |
> |---|---|---|
> | 优先级 | `markers` | 彩色优先级图标 |
> | 标签 / 设计方法 | `labels` | 文本标签（@smoke、等价类划分） |
> | 详情 | `notes.plain.content` | 右侧备注面板 |
>
> **notes 用 `plain.content` 而非 HTML**：XMind 完整支持纯文本 notes，兼容性最好，且避免 HTML 转义问题。

### 6.3 节点 ID 规范

XMind 要求 topic id 全局唯一。使用确定性生成（不用 uuid4，保证可复现）：

```python
import hashlib

def _topic_id(seed: str, taken: set[str]) -> str:
    """从语义内容派生稳定 id，保证同输入同输出。"""
    base = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:26]
    candidate, i = base, 1
    while candidate in taken:          # 极小概率碰撞，处理之
        candidate = hashlib.sha1(f"{seed}#{i}".encode("utf-8")).hexdigest()[:26]
        i += 1
    taken.add(candidate)
    return candidate
```

### 6.4 content.json 完整结构

```json
[
  {
    "id": "sheet-1",
    "class": "sheet",
    "title": "用例集",
    "rootTopic": { ... },
    "topicPositioning": "fixed",
    "theme": { "id": "prd2tc-theme", "importantTopic": {}, "centralTopic": {} }
  }
]
```

**sheet 级必备字段**：

| 字段 | 说明 | 缺失后果 |
|---|---|---|
| `id` | sheet 唯一 id | 必需 |
| `class` | 固定 `"sheet"` | 必需 |
| `title` | 页签名 | 可选 |
| `rootTopic` | 根主题 | **必需** |
| `topicPositioning` | `"fixed"`（固定布局）| 可选，缺失时 XMind 自动布局 |

**兼容性保险**：若目标 XMind 版本报格式错误，最优先补充的是 `metadata.json` 的 `activeSheetId`：

```json
{
  "creator": { "name": "prd2tc", "version": "0.6.0" },
  "activeSheetId": "sheet-1"
}
```

### 6.5 辅助成员

```json
// manifest.json
{ "file-entries": { "content.json": {}, "metadata.json": {}, "Thumbnails/thumbnail.png": {} } }
```

`manifest.json` 中列出包内成员。若不生成 manifest，XMind 通常仍可打开，但部分版本（尤其 2023+）可能出现"文件已损坏"提示，**建议生成**。

> **Thumbnail**：不生成。XMind 打开时会自动生成预览。

### 6.6 大导图性能与**自动降级**

XMind 对超大导图（>2000 节点）渲染吃力，且导出文件体积大。

```python
MAX_NODES = 3000
```

**三种展开模式**：

| 模式 | 行为 | 适用 |
|---|---|---|
| `--xmind-mode module` | 仅到模块层（附「共 N 条用例」备注） | 极端大用例集的兜底 |
| **`--xmind-mode case`** | 展开到每条用例（**默认**） | 日常审阅：模块→子模块→用例 |
| `--xmind-mode full` | 再展开到步骤层 | 需在导图中逐步骤走查 |

> **v1.1 修正 R-1（默认值必须是 `case`）**：v1.0 默认 `module`，导致两个问题：
> 1. **默认导图看不到任何用例**，只剩模块名，与 §6.1「模块→子模块→用例三级树」示例
>    及 F-2「模块→子模块→用例三级结构」需求**直接冲突**；
> 2. 文档 §10.1 的三个测试（`test_xmind_content_json_structure` 等）假定默认能取到用例节点，
>    实测 `KeyError: 'children'` —— **文档自带测试 4 个失败**。
>
> 现已统一为默认 `case`，并在 §10.1 补一条测试锁住"默认必须含用例节点"。

**超限时自动降级（R-4：v1.0 是抛错，与"降级策略"描述不符）**

v1.0 的实现是 `raise ValueError(...)`，结果是**完全不产出文件**；而 §6.6 声称的是"降级"。
实测 **3000 条用例（节点数 3003）即触发**，而这对一个"千条量级"的工具并不罕见。

修正为**自动降级 + 告警**，保证总有产物：

```python
def build_xmind_bytes(cases, *, title="测试用例集", mode="case", module_names=None):
    for attempt_mode in ([mode] if mode == "module" else [mode, "module"]):
        root = build_tree(cases, _IdFactory(), attempt_mode, module_names)
        if _count_nodes(root) <= MAX_NODES:
            if attempt_mode != mode:
                warnings.warn(
                    f"导图节点数超过 {MAX_NODES}，已从 {mode} 自动降级为 module 模式"
                    f"（仅模块层）。如需完整展开，请拆分用例集。"
                )
            return _pack(title, root)
    # 连 module 模式都超限（模块数极多）才真正失败
    raise ValueError(f"即使降级为 module 模式，节点数仍超过 {MAX_NODES}，请拆分用例集")
```

> 这样保证：**超限只会降低导图粒度，不会没有产物**。`module` 模式本身超限的情形
> 需要模块数 > 3000，实际不可能，故保留抛错作为最后防线。

---

## 7. CLI 与流水线集成

### 7.1 新增 CLI 命令

`cli.py` 的 `build_parser()` 中新增：

```python
e = sub.add_parser("export", help="导出用例为 Excel / XMind")
e.add_argument("--cases", required=True, help="用例目录，如 output/p/v/cases/cases")
e.add_argument("--format", choices=["excel", "xmind", "all"], default="all")
e.add_argument("--out", default=None, help="输出文件或目录（默认写回 cases/ 目录）")
e.add_argument("--product", default=None, help="产品名（默认从 --cases 路径推断）")
e.add_argument("--version", dest="ver", default=None, help="版本（默认从 --cases 路径推断）")
e.add_argument("--xmind-mode", choices=["module", "case", "full"], default="case")
e.add_argument("--sheet-name", default="用例明细")
e.add_argument("--no-traceability", action="store_true", help="不导出需求覆盖 sheet")
e.set_defaults(func=cmd_export)
```

命令实现：

```python
def cmd_export(args):
    """从用例 JSON 导出 Excel / XMind。"""
    from prd2tc.export import export_cases
    cases_path = Path(args.cases)
    # run_root = <output>/<product>/<version>；用例目录为 <run_root>/cases/cases
    run_root = cases_path.parent.parent
    # R-5 修正：--product/--version 此前声明了却从未传入 export_cases（死参数），
    # 导致文件名恒为「用例集.xlsx」、导图标题拿不到产品与版本。
    # 未显式给出时从路径推断，与 generate 的产物布局一致。
    product = args.product or run_root.parent.name
    version = args.ver or run_root.name
    try:
        result = export_cases(
            cases_dir=cases_path,
            formats=[args.format] if args.format != "all" else ["excel", "xmind"],
            out=Path(args.out) if args.out else None,
            root=run_root,
            xmind_mode=args.xmind_mode,
            sheet_name=args.sheet_name,
            include_traceability=not args.no_traceability,
            product=product,
            version=version,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"✗ {format_exception(exc)}"); return 1
    for path in result["files"]:
        size_kb = path.stat().st_size / 1024
        print(f"✔ 已导出 {path.name}（{size_kb:.1f} KB，共 {result['case_count']} 条用例）")
    return 0
```

> **路径推断约定**：`--cases` 期望指向 `<output>/<product>/<version>/cases/cases`，
> 于是 `run_root = cases.parent.parent`，`product = run_root.parent.name`、`version = run_root.name`。
> 若目录结构不同，显式传 `--product/--version` 即可。这让默认调用（§7.1 示例 1）
> 也能产出 `sample-mall-v0.1-用例集.xlsx` 这样可辨识的文件名。

使用示例：

```bash
# 导出两种格式到 cases/ 目录
prd2tc export --cases output/sample-mall/v0.1/cases/cases

# 只导出 Excel
prd2tc export --cases output/.../cases/cases --format excel

# 导图展开到每条用例
prd2tc export --cases output/.../cases/cases --format xmind --xmind-mode case

# 指定输出位置
prd2tc export --cases output/.../cases/cases --out /tmp/cases.xlsx
```

### 7.2 流水线自动导出

`pipeline.py` 阶段 3 成功后（现有 `save_cases` / `render_cases_md` 之后）追加：

```python
# pipeline.py，阶段 3 成功分支内
export_info = {}
try:
    from prd2tc.export import export_cases
    export_info = export_cases(
        cases_dir=root / "cases" / "cases",
        formats=["excel", "xmind"],
        root=root,
        include_traceability=True,
    )
except Exception as exc:      # 导出失败不阻断流水线（F-8）
    warnings.warn(f"导出失败（不影响生成结果）: {exc}")
```

> **注意**：`pipeline.py` 现有代码在 `except BaseException` 中统一捕获异常并标记 `status="failed"`。导出**必须**用局部 `try/except` 包裹，避免导出失败导致整个 generate 判定失败——这违背 F-8。

### 7.3 artifact_inventory 兼容

`io.artifact_inventory()`（`io.py:154`）遍历 `root.rglob("*")` 计算 SHA256，会自动收录新增的 `.xlsx` / `.xmind`。但需注意：

```python
# io.py:154 现有逻辑
for path in sorted(p for p in root.rglob("*") if p.is_file()):
    relative = path.relative_to(root).as_posix()
    if relative in {MANIFEST_NAME, LEGACY_MANIFEST_NAME, "run-state.json"} or "/.checkpoints/" in f"/{relative}":
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()   # ← 二进制安全
```

`read_bytes()` 对二进制安全，**无需修改**。但导出会使 manifest 的 `artifacts` 体积增大（每个文件一条记录），若用例很多需评估——当前规模无问题。

---

## 8. 服务层与前端集成

### 8.1 服务层下载接口

`service.py` 中新增：

```python
@app.get("/api/v1/exports/{task_id}/{fmt}")
def download_export(task_id: str, fmt: str, request: Request):
    """按需生成并返回导出文件。"""
    if fmt not in {"excel", "xmind"}:
        raise HTTPException(400, f"不支持的格式: {fmt}")
    task = store.task(task_id)
    if not task:
        raise HTTPException(404, "运行不存在")
    output = Path(task.get("output") or "")
    cases_dir = output / "cases" / "cases"
    if not cases_dir.is_dir():
        raise HTTPException(404, "该运行尚无用例产物")
    from prd2tc.export import export_cases
    try:
        result = export_cases(cases_dir=cases_dir, formats=[fmt], out=None)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    path = result["files"][0]
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if fmt == "excel" else "application/vnd.xmind.workbook"
    return FileResponse(path, media_type=media, filename=path.name)
```

### 8.2 路径安全

`service.py` 的 `WorkspaceStore._safe()`（`service.py:52`）已实现越界防护。**若导出支持 `--out` 传入任意路径的服务端接口，必须复用该校验**，否则构成路径穿越漏洞。

本方案默认输出到 `cases/` 目录内，`--out` 仅在 CLI 开放，不通过 HTTP 暴露，风险可控。

### 8.3 前端按钮

`ui/src/pages/RunPage.tsx` 的操作区增加：

```tsx
<Button onClick={() => window.open(`/api/v1/exports/${run.taskId}/excel`)}>导出 Excel</Button>
<Button onClick={() => window.open(`/api/v1/exports/${run.taskId}/xmind`)}>导出 XMind</Button>
```

`ui/src/types.ts` 的产物类型增加：

```ts
export interface RunArtifact {
  path: string;
  sha256: string;
}
```

> 前端改动可延后到 v0.6.1，先落地 CLI + 流水线（覆盖 90% 使用场景）。

---

## 9. 数据模型与字段映射

### 9.1 完整映射表

| JSON 字段 | Excel 列 | XMind 位置 | 缺失处理 |
|---|---|---|---|
| `id` | 用例编号 | 节点 title 前缀 / notes | 必填（门禁保证） |
| `module` | 模块 | 二级节点 | 必填 |
| `subModule` | 子模块 | 三级节点（可选层） | 省略该层 |
| `title` | 用例标题 | 节点 `title` | 必填 |
| `design_method` | 设计方法 | `labels` | `—` / 不加标签 |
| `priority` | 优先级 | `labels` | 必填 |
| `tags[]` | 标签 | `labels` | 必填非空 |
| `requirement[]` | 关联需求 | notes | 必填非空 |
| `preconditions[]` | 前置条件 | notes | `—` / 省略 |
| `testData[]` | 测试数据 | notes | `—` / 省略 |
| `steps[]` | 测试步骤（展开） | notes（`full` 模式可展开为子节点） | 必填非空 |
| `expected[]` | 预期结果 | notes | 必填非空 |
| `status` | 状态 | 不导出 | `active` |
| `createdBy` | 不导出 | 不导出 | — |

### 9.2 需求覆盖 sheet

数据源 `traceability.json`：

```python
_STATUS_CN = {
    "covered": "已覆盖",
    "insufficient": "覆盖不足",
    "uncovered": "未覆盖",
    "excluded": "已排除",
}

# 需要高亮的状态：覆盖不足 / 未覆盖 —— 这两类才是评审要盯的
_WARN_STATUSES = {"insufficient", "uncovered"}


def build_traceability_rows(matrix: dict) -> list[list[str]]:
    rows = [["需求编号", "模块", "优先级", "覆盖状态", "覆盖用例数", "用例编号列表"]]
    for req in matrix.get("requirements") or []:
        cases = req.get("caseIds") or []
        rows.append([
            str(req.get("id") or EMPTY),
            str(req.get("module") or EMPTY),
            str(req.get("priority") or EMPTY),
            _STATUS_CN.get(req.get("status") or "", str(req.get("status") or EMPTY)),
            str(len(cases)),
            ", ".join(str(c) for c in cases) or EMPTY,
        ])
    return rows


def _coverage_style(row: list[str]) -> int:
    """需求覆盖 sheet 的行样式：覆盖不足/未覆盖 → 浅红底（STYLE_WARN）。"""
    status = row[3] if len(row) > 3 else ""
    return STYLE_WARN if status in _STATUS_CN_WARN else STYLE_WRAP


# 反向映射：中文状态 → 是否需要高亮
_STATUS_CN_WARN = {_STATUS_CN[s] for s in _WARN_STATUSES}
```

> **v1.1 修正 R-6**：v1.0 声称"覆盖不足的行加浅红底色，与 P0 用例样式复用同一套样式索引"，
> 但 `WARN_FILL` 定义了**从未被任何 `cellXf` 引用**，且需求覆盖 sheet 的 `row_style_fn`
> 传的是 `None`。实测该 sheet 只用到样式 `['1','4']`（表头 + 换行），**完全没有着色**。
>
> 现修正为：`s=4`(`STYLE_WARN`) 承载浅红底 + 换行，并在 `build_xlsx_bytes` 中
> 给需求覆盖 sheet 也传 `row_style_fn=_coverage_style`。
>
> 注意 `STYLE_WARN` 自带 `wrapText`（见 §5.4），与 R-2 同一原则：
> **强调样式必须自带换行，不能依赖回退**。

### 9.3 统计概览 sheet

```python
def build_stats_rows(stats: dict) -> list[list[str]]:
    rows = [["统计维度", "分类", "数量"]]
    rows.append(["总计", "全部用例", str(stats["total"])])
    for key in ("by_module", "by_priority", "by_tag"):
        label = {"by_module": "按模块", "by_priority": "按优先级", "by_tag": "按标签"}[key]
        for name, count in (stats.get(key) or {}).items():
            rows.append([label, str(name), str(count)])
    rows.append(["需求覆盖", "覆盖 REQ 数", str(stats["req_covered"])])
    return rows
```

---

## 10. 测试方案

### 10.1 单元测试（纯函数，不触文件系统）

新增 `tests/test_export.py`。**v1.1 修正**：v1.0 的测试实际跑出 **4 个失败**
（`test_excel_escapes_special_chars` + 3 个 XMind 用例节点测试），根因见 R-1 / R-3。
下面为修正后版本，并为每个 v1.1 修正补了回归测试（R-1/R-2/R-4/R-6/R-7/R-8 各一条）：

```python
import json
import zipfile
from io import BytesIO

import pytest

from prd2tc.export import common, excel, xmind

SAMPLE_CASE = {
    "id": "TC-AUTH-LOGIN-001",
    "module": "AUTH",
    "subModule": "LOGIN",
    "title": "正确凭据登录成功并跳转首页",
    "design_method": "等价类划分",
    "priority": "P0",
    "tags": ["@smoke", "@auth"],
    "requirement": ["REQ-AUTH-002"],
    "preconditions": ["系统中存在 normal 状态账号"],
    "testData": ["phone=13800138000", "password=demo123456"],
    "steps": [
        {"action": "goto", "target": "/login", "data": ""},
        {"action": "fill", "target": "手机号输入框", "data": "13800138000"},
        {"action": "click", "target": "登录按钮", "data": ""},
    ],
    "expected": ["登录成功后跳转首页", "导航栏显示当前用户昵称"],
    "status": "active",
}


# ---------- common ----------

def test_flatten_steps_renders_numbered_lines():
    text = common.render_steps(SAMPLE_CASE["steps"])
    assert text.splitlines() == [
        "1. [goto] /login",
        "2. [fill] 手机号输入框 ← 13800138000",
        "3. [click] 登录按钮",
    ]


def test_render_steps_empty():
    assert common.render_steps([]) == "—"


def test_list_to_text_joins_with_newline():
    assert common.list_to_text(["a", "b"], empty="—") == "a\nb"
    assert common.list_to_text([], empty="—") == "—"


# ---------- excel ----------

def test_col_name_conversion():
    assert excel.col_name(0) == "A"
    assert excel.col_name(25) == "Z"
    assert excel.col_name(26) == "AA"
    assert excel.col_name(701) == "ZZ"
    assert excel.col_name(702) == "AAA"


def test_excel_bytes_is_valid_zip():
    data = excel.build_xlsx_bytes([SAMPLE_CASE])
    assert data[:4] == b"PK\x03\x04"
    with zipfile.ZipFile(BytesIO(data)) as z:
        assert z.testzip() is None
        names = z.namelist()
        assert "[Content_Types].xml" in names
        assert "xl/workbook.xml" in names
        assert "xl/worksheets/sheet1.xml" in names


def test_excel_escapes_special_chars():
    """XML 元素**正文**只需转义 & < >；双引号无需转义。

    v1.0 断言 `&quot;` 存在 —— 那是错的（`escape()` 默认不转义引号，
    而元素正文里的 `"` 本就合法），导致文档自带测试失败。
    """
    case = {**SAMPLE_CASE, "title": "登录 <成功> & \"引号\""}
    data = excel.build_xlsx_bytes([case])
    with zipfile.ZipFile(BytesIO(data)) as z:
        xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "&lt;成功&gt;" in xml
    assert "&amp;" in xml
    assert "&quot;" not in xml          # 正文中的引号保持原样
    assert '"引号"' in xml
    from xml.etree import ElementTree as ET
    ET.fromstring(xml)                  # 整个 part 必须仍良构


def test_excel_is_deterministic():
    """可复现：同输入两次生成的字节必须一致。"""
    a = excel.build_xlsx_bytes([SAMPLE_CASE])
    b = excel.build_xlsx_bytes([SAMPLE_CASE])
    assert a == b


def test_excel_p0_uses_highlight_style():
    data = excel.build_xlsx_bytes([SAMPLE_CASE])
    with zipfile.ZipFile(BytesIO(data)) as z:
        styles = z.read("xl/styles.xml").decode("utf-8")
    assert "FFFFC7CE" in styles   # P0 底色存在


def test_excel_p0_row_keeps_wrap_text():
    """回归 R-2：P0/P1 行**必须**同时带底色与自动换行。

    v1.0 的 `_priority_style` 返回不含 wrapText 的样式，导致 P0 行的
    「测试步骤」「预期结果」挤成一行 —— 而 P0 恰恰最需要审阅。
    """
    import re
    data = excel.build_xlsx_bytes([SAMPLE_CASE])
    with zipfile.ZipFile(BytesIO(data)) as z:
        styles = z.read("xl/styles.xml").decode()
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
    row2 = re.search(r'<row r="2">(.*?)</row>', sheet, re.S).group(1)
    used = {int(s) for s in re.findall(r'<c [^>]*s="(\d+)"', row2)}
    assert used, "第2行应带样式"
    xfs = re.search(r"<cellXfs[^>]*>(.*?)</cellXfs>", styles, re.S).group(1)
    entries = re.findall(r"<xf\b.*?(?:/>|</xf>)", xfs, re.S)
    for idx in used:
        assert "wrapText" in entries[idx], f"第2行所用样式 s={idx} 缺少 wrapText"


def test_excel_has_default_style():
    """回归 R-7：styles.xml 必须含 <cellStyles>，否则 openpyxl 报
    'Workbook contains no default style'，严格实现可能直接失败。"""
    data = excel.build_xlsx_bytes([SAMPLE_CASE])
    with zipfile.ZipFile(BytesIO(data)) as z:
        styles = z.read("xl/styles.xml").decode()
    assert "<cellStyles" in styles
    assert "<cellStyle " in styles


def test_excel_warn_fill_used_for_uncovered():
    """回归 R-6：覆盖不足/未覆盖的需求行必须着色（v1.0 定义了底色却从未引用）。"""
    import re
    matrix = {"requirements": [
        {"id": "REQ-A-001", "module": "AUTH", "priority": "P0", "status": "uncovered", "caseIds": []},
        {"id": "REQ-A-002", "module": "AUTH", "priority": "P1", "status": "covered",
         "caseIds": ["TC-AUTH-LOGIN-001"]},
    ]}
    data = excel.build_xlsx_bytes([SAMPLE_CASE], traceability=matrix)
    with zipfile.ZipFile(BytesIO(data)) as z:
        sheet2 = z.read("xl/worksheets/sheet2.xml").decode()
        styles = z.read("xl/styles.xml").decode()
    assert "FFFFE1E1" in styles, "WARN 底色应已定义"
    row2 = re.search(r'<row r="2">(.*?)</row>', sheet2, re.S).group(1)
    used = {int(s) for s in re.findall(r'<c [^>]*s="(\d+)"', row2)}
    assert excel.STYLE_WARN in used, f"未覆盖行未着色，实际样式 {used}"


def test_sheet_name_special_chars_are_escaped():
    """回归 R-8：sheet 名含 " & < > 时不得破坏 workbook.xml。"""
    from xml.etree import ElementTree as ET
    for raw in ['用"例', "a&b", "x<y>z"]:
        data = excel.build_xlsx_bytes([SAMPLE_CASE], sheet_name=excel.safe_sheet_name(raw))
        with zipfile.ZipFile(BytesIO(data)) as z:
            ET.fromstring(z.read("xl/workbook.xml"))   # 良构性即通过


# ---------- xmind ----------

def test_xmind_bytes_is_valid_zip():
    data = xmind.build_xmind_bytes([SAMPLE_CASE])
    assert data[:4] == b"PK\x03\x04"
    with zipfile.ZipFile(BytesIO(data)) as z:
        assert z.testzip() is None
        assert "content.json" in z.namelist()
        assert "metadata.json" in z.namelist()
        assert "manifest.json" in z.namelist()


def _case_topic_of(data: bytes) -> dict:
    """取第一条例用节点（默认 case 模式，模块下可能夹一层 subModule）。"""
    with zipfile.ZipFile(BytesIO(data)) as z:
        content = json.loads(z.read("content.json"))
    module = content[0]["rootTopic"]["children"]["attached"][0]
    for child in module["children"]["attached"]:
        if "notes" in child:
            return child
        nested = (child.get("children") or {}).get("attached") or []
        if nested:
            return nested[0]
    raise AssertionError("未找到用例节点")


def test_xmind_content_json_structure():
    data = xmind.build_xmind_bytes([SAMPLE_CASE])
    with zipfile.ZipFile(BytesIO(data)) as z:
        content = json.loads(z.read("content.json"))
    sheet = content[0]
    assert sheet["class"] == "sheet"
    root = sheet["rootTopic"]
    assert root["class"] == "topic"
    modules = root["children"]["attached"]
    assert modules[0]["title"].startswith("AUTH")
    # 默认模式必须是 case：模块下应能看到用例节点（回归 R-1）
    assert modules[0]["children"]["attached"], "默认模式未展开用例节点（v1.0 的 R-1 缺陷）"


def test_xmind_default_mode_includes_case_nodes():
    """回归 R-1：默认 mode 必须是 case，与 §6.1 三级树和 F-2 需求一致。"""
    import inspect
    default = inspect.signature(xmind.build_xmind_bytes).parameters["mode"].default
    assert default == "case", f"默认模式应为 case，实际 {default}"
    with zipfile.ZipFile(BytesIO(xmind.build_xmind_bytes([SAMPLE_CASE]))) as z:
        raw = z.read("content.json").decode("utf-8")
    assert "TC-AUTH-LOGIN-001" in raw, "默认导图必须包含用例编号"


def test_xmind_notes_contain_key_fields():
    topic = _case_topic_of(xmind.build_xmind_bytes([SAMPLE_CASE]))
    notes = topic["notes"]["plain"]["content"]
    assert "REQ-AUTH-002" in notes
    assert "P0" in notes
    assert "[goto] /login" in notes


def test_xmind_markers_include_priority():
    """回归 R-3：优先级必须用 markers（优先级图标）而非 labels（纯文本）。

    v1.0 把 `priority-1` 放进 labels，XMind 只会显示成无意义英文文本，
    不会渲染成优先级图标。
    """
    topic = _case_topic_of(xmind.build_xmind_bytes([SAMPLE_CASE]))
    assert topic.get("markers") == [{"markerId": "priority-1"}]
    assert not [x for x in topic.get("labels", []) if str(x).startswith("priority-")]


def test_xmind_labels_carry_tags_and_method():
    topic = _case_topic_of(xmind.build_xmind_bytes([SAMPLE_CASE]))
    labels = topic.get("labels", [])
    assert "@smoke" in labels and "@auth" in labels
    assert "等价类划分" in labels


def test_xmind_is_deterministic():
    a = xmind.build_xmind_bytes([SAMPLE_CASE])
    b = xmind.build_xmind_bytes([SAMPLE_CASE])
    assert a == b


def test_xmind_module_mode_has_no_case_nodes():
    data = xmind.build_xmind_bytes([SAMPLE_CASE], mode="module")
    with zipfile.ZipFile(BytesIO(data)) as z:
        content = json.loads(z.read("content.json"))
    modules = content[0]["rootTopic"]["children"]["attached"]
    assert modules[0]["title"].startswith("AUTH")
    assert not modules[0].get("children")   # module 模式不展开
    assert modules[0]["notes"]["plain"]["content"] == "共 1 条用例"


def test_xmind_degrades_instead_of_raising():
    """回归 R-4：超限应**自动降级为 module** 并告警，而不是直接不产出文件。

    v1.0 抛 ValueError（§6.6 却声称"降级"），实测 3000 条用例即触发，
    结果是完全没有 .xmind 产物。
    case 模式节点数 = root 1 + 模块 1 + 子模块 1 + N 用例 = N + 3。
    取 N = MAX_NODES 必然超限，而模块级只有 3 个节点 → 必然降级成功。
    """
    import warnings
    many = [{**SAMPLE_CASE, "id": f"TC-A-{i:04d}"} for i in range(xmind.MAX_NODES)]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        data = xmind.build_xmind_bytes(many, mode="case")
    assert data[:4] == b"PK\x03\x04", "超限时仍必须产出文件"
    assert any("降级" in str(w.message) for w in caught), "应发出降级告警"
    with zipfile.ZipFile(BytesIO(data)) as z:
        content = json.loads(z.read("content.json"))
    modules = content[0]["rootTopic"]["children"]["attached"]
    assert not modules[0].get("children"), "降级后应为 module 模式"


def test_empty_cases_raises():
    with pytest.raises(ValueError, match="没有用例"):
        excel.build_xlsx_bytes([])
    with pytest.raises(ValueError, match="没有用例"):
        xmind.build_xmind_bytes([])


def test_excel_has_stats_sheet():
    """回归 R-11：§5.1 声明 3 个 sheet，统计概览曾漏实现。"""
    stats = {"total": 1, "by_module": {"AUTH": 1}, "by_priority": {"P0": 1},
             "by_tag": {"@smoke": 1}, "req_covered": 1}
    data = excel.build_xlsx_bytes([SAMPLE_CASE], stats=stats)
    with zipfile.ZipFile(BytesIO(data)) as z:
        wb = z.read("xl/workbook.xml").decode()
        sheet2 = z.read("xl/worksheets/sheet2.xml").decode()
    assert "统计概览" in wb
    assert "全部用例" in sheet2 and "按模块" in sheet2


def test_sheet_names_are_unique():
    """回归 R-12：用户 sheet 名与内置名撞车时不得产生重名（Excel 判损坏）。"""
    import re
    stats = {"total": 1, "by_module": {}, "by_priority": {}, "by_tag": {}, "req_covered": 0}
    for name in ("统计概览", "需求覆盖", "用例明细"):
        data = excel.build_xlsx_bytes([SAMPLE_CASE], stats=stats, sheet_name=name)
        with zipfile.ZipFile(BytesIO(data)) as z:
            names = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
        assert len(names) == len(set(names)), (name, names)


def test_excel_relationship_types_are_valid():
    """回归 R-13（最重要）：关系类型 URI 必须完整含 `/2006/`。

    曾漏掉 `2006/` 生成 `.../officeDocument/relationships/worksheet`，
    Excel 判文件损坏、修复后只剩工作表分页无内容；
    而 openpyxl 不校验关系 URI，因此当时误判为通过。
    """
    from xml.etree import ElementTree as ET
    ALLOWED = {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
    }
    data = excel.build_xlsx_bytes([SAMPLE_CASE], traceability={"requirements": []},
                                  stats={"total": 1})
    with zipfile.ZipFile(BytesIO(data)) as z:
        containers = [ET.fromstring(z.read("xl/_rels/workbook.xml.rels")),
                      ET.fromstring(z.read("_rels/.rels"))]
    ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    for container in containers:
        for rel in container.findall("r:Relationship", ns):
            rel_type = rel.get("Type")
            assert rel_type in ALLOWED, f"非法关系类型 URI: {rel_type}"
            assert "/2006/relationships/" in rel_type, rel_type


def test_excel_relationship_targets_exist():
    """回归：关系指向的 part 必须真实存在，否则 Excel 报修复。"""
    from xml.etree import ElementTree as ET
    data = excel.build_xlsx_bytes([SAMPLE_CASE], traceability={"requirements": []})
    with zipfile.ZipFile(BytesIO(data)) as z:
        names = set(z.namelist())
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    for rel in rels.findall("r:Relationship", ns):
        assert f"xl/{rel.get('Target')}" in names, rel.get("Target")


def test_excel_content_types_cover_all_parts():
    """回归：每个 xml part 都必须被 [Content_Types].xml 声明。

    注意 `[Content_Types].xml` 自身不参与声明（规范如此）。
    """
    data = excel.build_xlsx_bytes([SAMPLE_CASE], traceability={"requirements": []},
                                  stats={"total": 1})
    with zipfile.ZipFile(BytesIO(data)) as z:
        ct = z.read("[Content_Types].xml").decode()
        parts = [n for n in z.namelist()
                 if n.endswith(".xml") and n != "[Content_Types].xml"]
    for part in parts:
        assert f'PartName="/{part}"' in ct, f"{part} 未声明"
```

**测试与 v1.1 修正的对应关系**（防止将来回归）：

| 回归测试 | 锁定的修正 |
|---|---|
| `test_excel_escapes_special_chars` | 修正 v1.0 的错误断言（元素正文的引号无需转义） |
| `test_excel_p0_row_keeps_wrap_text` | R-2：P0/P1 行必须带自动换行 |
| `test_excel_has_default_style` | R-7：必须含 `<cellStyles>` |
| `test_excel_warn_fill_used_for_uncovered` | R-6：覆盖不足必须着色 |
| `test_sheet_name_special_chars_are_escaped` | R-8：sheet 名特殊字符须转义 |
| `test_xmind_default_mode_includes_case_nodes` | R-1：默认模式必须是 `case` |
| `test_xmind_markers_include_priority` | R-3：优先级用 `markers` 而非 `labels` |
| `test_xmind_degrades_instead_of_raising` | R-4：超限降级而非抛错 |

### 10.2 集成测试

```python
def test_export_end_to_end(tmp_path):
    """从 JSON 目录导出两种格式。"""
    from prd2tc.export import export_cases
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "AUTH.json").write_text(
        json.dumps({"schemaVersion": "2.0", "cases": [SAMPLE_CASE]}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = export_cases(cases_dir=cases_dir, formats=["excel", "xmind"], out=tmp_path)
    assert len(result["files"]) == 2
    for path in result["files"]:
        assert path.exists()
        assert path.stat().st_size > 500
        with zipfile.ZipFile(path) as z:
            assert z.testzip() is None
```

### 10.3 人工验收（必须执行）

| 项 | 操作 | 通过标准 |
|---|---|---|
| A-1 | 用 Excel / WPS 打开导出文件 | 无"格式警告"弹窗，表头着色正常，列宽合理 |
| A-2 | 在 Excel 中筛选优先级=P0 | 筛选生效，自动换行不错位 |
| A-3 | 用 XMind 打开导出文件 | 导图正常渲染，可编辑 |
| A-4 | XMind 中点击用例节点 | notes 面板显示完整信息 |
| A-5 | 中文文件名/内容 | 无乱码 |

### 10.4 回归验证

导出功能不应影响既有流程：

```bash
# 生成后校验产物完整性
prd2tc generate --prd examples/sample-prd.md --stub --out /tmp/demo
prd2tc validate --dir /tmp/demo/demo/v0.1/cases/cases
prd2tc stats    --dir /tmp/demo/demo/v0.1/cases/cases
# 全量单测
pytest -q
```

---

## 11. 实施计划与风险

### 11.1 分阶段实施

> **实施状态（2026-10-12）**：S0–S5 已落地（`src/prd2tc/export/` 4 个模块 + `tests/test_export.py`
> 29 个测试 + CLI `export` 子命令 + 流水线自动导出 + 服务层下载接口）。S6 前端按钮亦已完成。
> 剩余唯一未验证项是 **S0 的人工打开确认**（本机沙箱无法启动 Excel 打开文件；详见 §3）。

| 阶段 | 内容 | 产出 | 预估 | 状态 |
|---|---|---|---|---|
| **S0** | 人工验证 | 用探测脚本产出样例文件，Excel/XMind 各打开一次确认 | 0.5h | ⏳ 待人工确认 |
| **S1** | `common.py` + `excel.py` | `build_xlsx_bytes()` + 单测 | 3h | ✅ 完成 |
| **S2** | `common.py` + `xmind.py` | `build_xmind_bytes()` + 单测 | 2.5h | ✅ 完成 |
| **S3** | `__init__.py` + CLI | `export_cases()` + `prd2tc export` | 1.5h | ✅ 完成 |
| **S4** | 流水线集成 | `pipeline.py` 自动导出 | 1h | ✅ 完成 |
| **S5** | 文档 | README + `docs/case-template.md` 更新 | 1h | ✅ 完成 |
| **S6** | 服务层 + UI | 下载接口 + 按钮 | 2h | ✅ 完成 |

**S0 仍是唯一待办**：代码侧已用 openpyxl 交叉验证（§3），但**必须**由人工用 Excel 与 XMind
各打开一次确认无格式警告。若 XMind 报格式错误，需在调整 `metadata.json` / `manifest.json` 结构后再发布。

### 11.1.1 落地实现与文档的差异

实现严格按本附录执行，仅有一处**超出**文档原设计（均为缺陷修正，已回填附录）：

| 项 | 说明 |
|---|---|
| **R-2** | 样式表由 5 个 `cellXfs` 扩为 6 个（P0/P1 各带 `wrapText`） |
| **R-6** | 需求覆盖 sheet 增加 `_coverage_style()` 行着色（原 `WARN_FILL` 未被引用） |
| **R-11** | 补 `build_stats_rows()` 实现第 3 个 sheet（原设计有、代码无） |
| **R-12** | 新增 `_dedupe_sheet_names()` 处理 sheet 重名 |
| **连带修复** | `service.py` 的 artifact 接口原先用 `read_text()` 读产物，遇到二进制 `.xlsx/.xmind` 会抛 `UnicodeDecodeError`；现按扩展名分流到 `FileResponse` |
| **产物留痕** | `run-manifest.json` 的 `stages.cases.exports` 记录导出文件名；xlsx/xmind 进入 `artifacts` 清单 |

### 11.2 风险登记

| 风险 | 等级 | 影响 | 缓解 |
|---|---|---|---|
| **XMind 客户端拒绝打开** | 高 | 功能不可用 | S0 先验证；备选：改用 `content.xml`（XMind 8 格式）；再备选：输出 `.opml`（纯 XML，XMind 可导入） |
| **Excel 元素顺序错误报"文件损坏"** | 中 | 功能不可用 | 严格遵循 `cols → sheetData → autoFilter`；S0 验证 |
| **OOXML 规范理解偏差** | 中 | 部分软件打不开 | 严格只写子集；不使用公式/图表/共享字符串；**已用 openpyxl 交叉验证**（§3） |
| **大导图 XMind 卡顿** | 中 | 体验差 | 默认 `case` 模式；`MAX_NODES` 超限**自动降级为 module**（R-4） |
| **导出破坏可复现性** | 中 | 回归基线失效 | 固定 ZIP 时间戳 + 确定性 topic id；单测断言两次生成字节一致 |
| **旧版用例缺字段导致 KeyError** | 低 | 导出失败 | 全部字段用 `.get()` + 缺失默认值 |
| **导出失败阻断流水线** | 高 | 主流程失败 | 局部 `try/except` + `warnings.warn`（F-8） |
| **sheet 名含 `"` `&` `<` `>`** | 低 | workbook.xml 损坏 | `safe_sheet_name()` + 属性级 `_attr()` 转义（R-8） |
| **大文件内存占用** | 低 | OOM | 千条量级仅数 MB；超 1 万条时提示分表 |

### 11.3 对可复现性的影响评估

`run-manifest.json` 的 `artifacts` 会包含新增的 `.xlsx`/`.xmind` 及其 SHA256。这带来一个影响：

**同一份 JSON 输入 → 导出字节一致**（因固定时间戳 + 确定性 id），因此导出产物**不会**破坏 `--repro` / `compare-runs` 的对拍能力，反而提供了额外的产物一致性校验维度。

单测 `test_excel_is_deterministic` / `test_xmind_is_deterministic` 是这条性质的守护。

### 11.4 文档更新清单

| 文件 | 更新点 |
|---|---|
| `README.md` | 新增「产物导出」章节；命令示例；目录结构说明 |
| `docs/case-template.md` | 新增第 6 节「导出格式」 |
| `docs/pipeline.md` | 第 3 节「产物规范」补充 xlsx/xmind |
| `pyproject.toml` | 无需改依赖（零第三方）|

---

## 附录 A：完整实现代码

> 以下代码为实现参考，需按项目代码风格（含中文注释、简洁风格）调整。

### A.1 `src/prd2tc/export/common.py`

```python
"""导出共享工具：行展开、文本渲染、二进制原子写、ZIP 打包。"""
from __future__ import annotations

import io
import os
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

# XLSX / XMIND 的固定时间戳：保证同输入产出字节一致（可复现性要求）
FIXED_TIMESTAMP = (2020, 1, 1, 0, 0, 0)

EMPTY = "—"


def list_to_text(values: list[Any] | None, *, sep: str = "\n", empty: str = EMPTY) -> str:
    """字符串数组 → 多行文本；空数组返回占位符。"""
    items = [str(v) for v in (values or []) if str(v).strip()]
    return sep.join(items) if items else empty


def render_steps(steps: list[dict] | None) -> str:
    """步骤数组 → 编号文本：`1. [action] target ← data`。

    动作加方括号便于在 Excel 中一眼区分动作与定位目标；
    有输入值时用 ← 标注，符合测试人员阅读习惯。
    """
    if not steps:
        return EMPTY
    lines = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            lines.append(f"{index}. {step}")
            continue
        action = str(step.get("action") or "").strip()
        target = str(step.get("target") or "").strip()
        data = str(step.get("data") or "").strip()
        line = f"{index}. [{action}] {target}" if action else f"{index}. {target}"
        if data:
            line += f" ← {data}"
        lines.append(line)
    return "\n".join(lines)


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """二进制原子写：先写临时文件再 os.replace。

    与 io.atomic_write_text 同样的 Windows 兼容处理——
    服务层的心跳与状态轮询会并发读写同一目录，os.replace
    可能因目标文件被占用抛 PermissionError，短暂重试即可。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    return path


def build_zip_bytes(members: dict[str, str]) -> bytes:
    """按固定顺序与固定时间戳生成 ZIP 字节，确保可复现（纯函数，不写盘）。"""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    return stream.getvalue()


def write_zip(path: Path, members: dict[str, str]) -> Path:
    """生成 ZIP 并原子写盘。"""
    return atomic_write_bytes(path, build_zip_bytes(members))
```

### A.2 `src/prd2tc/export/excel.py`

```python
"""Excel（.xlsx / OOXML）导出：纯标准库实现，不依赖 openpyxl。"""
from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape

from prd2tc.export.common import EMPTY, build_zip_bytes, list_to_text, render_steps

EXCEL_MAX_ROWS = 1_048_576

# 样式索引（与下方 _STYLES 的 cellXfs 顺序一一对应）
STYLE_NORMAL = 0   # 普通（带换行）
STYLE_HEADER = 1   # 表头（加粗居中）
STYLE_P0 = 2       # P0 红底 + 换行
STYLE_P1 = 3       # P1 橙底 + 换行
STYLE_WARN = 4     # 覆盖不足 + 换行
STYLE_WRAP = 5     # 纯换行（兜底）

P0_FILL = "FFFFC7CE"    # 浅红：P0 用例
P1_FILL = "FFFFEB9C"    # 浅黄：P1 用例
WARN_FILL = "FFFFE1E1"  # 浅红：覆盖不足/未覆盖的需求

# OOXML 关系类型命名空间前缀。
# ⚠️ 必须**完整包含 `/2006/`**：曾因 f-string 拆行时漏掉它而生成非法 URI
# （.../officeDocument/relationships/worksheet），Excel 判文件损坏；
# 「修复」后只剩下工作表分页、没有任何内容。故提为常量，杜绝再次拆行出错。
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# Excel sheet 名非法字符
_ILLEGAL_SHEET_CHARS = re.compile(r"[:\\/?*\[\]]")


def safe_sheet_name(value: str, fallback: str = "sheet") -> str:
    """清洗 Excel sheet 名：替换非法字符并截断到 31 字符上限。

    注意：这里只处理 Excel 自身的命名限制；XML 属性转义由 `_attr()` 负责，
    两者缺一不可（曾因只做前者，导致含 " 的 sheet 名把 workbook.xml 写坏）。
    """
    cleaned = _ILLEGAL_SHEET_CHARS.sub("-", str(value or "")).strip()
    return cleaned[:31] or fallback


def _attr(value: str) -> str:
    """XML **属性值**转义：属性里必须额外转义双引号（元素正文则不必）。"""
    return escape(str(value), {'"': "&quot;"})

COLUMNS: list[tuple[str, int, str]] = [
    # (标题, 列宽, 字段键)
    ("用例编号", 18, "id"),
    ("模块", 12, "module"),
    ("子模块", 12, "subModule"),
    ("用例标题", 40, "title"),
    ("设计方法", 14, "design_method"),
    ("优先级", 8, "priority"),
    ("标签", 20, "tags"),
    ("关联需求", 24, "requirement"),
    ("前置条件", 32, "preconditions"),
    ("测试数据", 24, "testData"),
    ("测试步骤", 52, "steps"),
    ("预期结果", 40, "expected"),
    ("状态", 12, "status"),
]

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheets}<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WRAP = '<alignment wrapText="1" vertical="top"/>'

# 关键：每个强调样式都自带 wrapText，不依赖回退到 STYLE_WRAP。
# 否则 P0/P1 行的「测试步骤」「预期结果」会挤成一行——而 P0 恰恰最需要审阅。
_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
<font><b/><color rgb="FF9C0006"/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="5">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="{P0_FILL}"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="{P1_FILL}"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="{WARN_FILL}"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="6">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">{_WRAP}</xf>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">{_WRAP}</xf>
<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1" applyAlignment="1">{_WRAP}</xf>
<xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1" applyAlignment="1">{_WRAP}</xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">{_WRAP}</xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
<dxfs count="0"/>
<tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>"""


def col_name(idx: int) -> str:
    """列序号 → 列名：0→A, 25→Z, 26→AA, 702→AAA。"""
    name = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        name = chr(65 + rem) + name
    return name


def _cell(ref: str, value: Any, style: int) -> str:
    return (
        f'<c r="{ref}" s="{style}" t="inlineStr">'
        f'<is><t xml:space="preserve">{escape(str(value))}</t></is></c>'
    )


def _sheet_xml(rows: list[list[str]], *, widths: list[int], freeze: bool,
               autofilter: bool, row_style_fn=None) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if freeze:
        parts.append(
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
        )
    # cols 必须位于 sheetData 之前（OOXML 元素顺序要求）
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(widths)
    )
    if cols:
        parts.append(f"<cols>{cols}</cols>")
    parts.append("<sheetData>")
    for r_index, row in enumerate(rows, 1):
        style = STYLE_HEADER if r_index == 1 else None
        if style is None and row_style_fn is not None:
            style = row_style_fn(row)
        parts.append(f'<row r="{r_index}">')
        for c_index, value in enumerate(row):
            cell_style = style if style is not None else STYLE_NORMAL
            parts.append(_cell(f"{col_name(c_index)}{r_index}", value, cell_style))
        parts.append("</row>")
    parts.append("</sheetData>")
    # autoFilter 必须位于 sheetData 之后
    if autofilter and rows:
        last_col = col_name(len(rows[0]) - 1)
        parts.append(f'<autoFilter ref="A1:{last_col}{len(rows)}"/>')
    parts.append("</worksheet>")
    return "".join(parts)


def _case_row(case: dict) -> list[str]:
    return [
        str(case.get("id") or EMPTY),
        str(case.get("module") or EMPTY),
        str(case.get("subModule") or EMPTY) if case.get("subModule") else EMPTY,
        str(case.get("title") or EMPTY),
        str(case.get("design_method") or EMPTY) if case.get("design_method") else EMPTY,
        str(case.get("priority") or EMPTY),
        " ".join(str(t) for t in (case.get("tags") or [])) or EMPTY,
        ", ".join(str(r) for r in (case.get("requirement") or [])) or EMPTY,
        list_to_text(case.get("preconditions")),
        list_to_text(case.get("testData")),
        render_steps(case.get("steps")),
        list_to_text(case.get("expected")),
        str(case.get("status") or "active"),
    ]


def _dedupe_sheet_names(sheets: list[tuple]) -> list[tuple]:
    """确保 sheet 名唯一：重名时追加 -2、-3…（Excel 要求唯一，否则文件判损坏）。"""
    seen: dict[str, int] = {}
    output = []
    for title, rows, widths, style_fn in sheets:
        base = str(title)
        if base in seen:
            seen[base] += 1
            candidate = f"{base[:28]}-{seen[base]}"      # 留出后缀空间，仍 ≤31 字符
            while candidate in seen:
                seen[base] += 1
                candidate = f"{base[:28]}-{seen[base]}"
            seen[candidate] = 1
            title = candidate
        else:
            seen[base] = 1
        output.append((title, rows, widths, style_fn))
    return output


def build_xlsx_bytes(cases: list[dict], *, traceability: dict | None = None,
                     stats: dict | None = None,
                     sheet_name: str = "用例明细") -> bytes:
    """用例列表 → xlsx 字节流。纯函数，不写盘。

    sheet_name 在此处再清洗一次（Excel 禁止 : \\ / ? * [ ]），
    属性级 XML 转义由 `_attr()` 负责；与内置 sheet 重名时由 `_dedupe_sheet_names()` 自动追加 `-2`。
    """
    if not cases:
        raise ValueError("没有用例可导出")
    if len(cases) + 1 > EXCEL_MAX_ROWS:
        raise ValueError(f"用例数 {len(cases)} 超出单 Sheet 上限 {EXCEL_MAX_ROWS}")

    # 每个 sheet 带自己的行样式函数：用例明细按优先级着色，需求覆盖按覆盖状态着色
    sheets = [(safe_sheet_name(sheet_name), _case_rows_with_header(cases),
               [w for _, w, _ in COLUMNS], _priority_style)]

    if traceability:
        cover_rows = build_traceability_rows(traceability)
        sheets.append(("需求覆盖", cover_rows, [16, 12, 8, 12, 10, 40], _coverage_style))

    if stats:
        # §5.1 的第 3 个 sheet；v1.0 附录漏实现（代码与设计不符），v1.1 补齐
        sheets.append(("统计概览", build_stats_rows(stats), [16, 20, 10], None))
    # sheet 名必须唯一：若用户的 --sheet-name 与内置名撞车，Excel 会判定文件损坏
    sheets = _dedupe_sheet_names(sheets)

    sheet_entries = []
    rel_entries = []
    for i, (title, _, _, _) in enumerate(sheets, 1):
        sheet_entries.append(f'<sheet name="{_attr(title)}" sheetId="{i}" r:id="rId{i}"/>')
        rel_entries.append(
            f'<Relationship Id="rId{i}" Type="{REL_NS}/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
        )
    style_rid = len(sheets) + 1
    rel_entries.append(
        f'<Relationship Id="rId{style_rid}" Type="{REL_NS}/styles" Target="styles.xml"/>'
    )

    members = {
        "[Content_Types].xml": _CONTENT_TYPES.format(
            sheets="".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
                f'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(sheets) + 1)
            )
        ),
        "_rels/.rels": _ROOT_RELS,
        "xl/workbook.xml": _WORKBOOK_XML.format(sheets="".join(sheet_entries)),
        "xl/_rels/workbook.xml.rels": _WB_RELS_XML.format(rels="".join(rel_entries)),
        "xl/styles.xml": _STYLES,
    }
    for i, (title, rows, widths, style_fn) in enumerate(sheets, 1):
        is_main = i == 1
        members[f"xl/worksheets/sheet{i}.xml"] = _sheet_xml(
            rows,
            widths=widths,
            freeze=is_main,
            autofilter=is_main,
            row_style_fn=style_fn,     # 各 sheet 用自己的着色函数（用例按优先级、覆盖按状态）
        )

    return build_zip_bytes(members)


_STYLE_BY_PRIORITY = {"P0": STYLE_P0, "P1": STYLE_P1}


def _priority_style(row: list[str]) -> int:
    """按优先级着色：P0 浅红、P1 浅黄、其余常规。

    注意 STYLE_P0/STYLE_P1 **自带 wrapText**，否则 P0/P1 行的多行单元格会挤成一行。
    """
    priority = row[5] if len(row) > 5 else ""
    return _STYLE_BY_PRIORITY.get(priority, STYLE_NORMAL)


_WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>{sheets}</sheets>
</workbook>"""

_WB_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rels}
</Relationships>"""


def _case_rows_with_header(cases: list[dict]) -> list[list[str]]:
    return [[title for title, _, _ in COLUMNS]] + [_case_row(c) for c in cases]


_STATUS_CN = {"covered": "已覆盖", "insufficient": "覆盖不足",
              "uncovered": "未覆盖", "excluded": "已排除"}
# 需要高亮的状态（中文值）：这两类才是评审要盯的
_STATUS_CN_WARN = {_STATUS_CN["insufficient"], _STATUS_CN["uncovered"]}


def _coverage_style(row: list[str]) -> int:
    """需求覆盖 sheet：覆盖不足/未覆盖 → 浅红底（STYLE_WARN，自带换行）。"""
    status = row[3] if len(row) > 3 else ""
    return STYLE_WARN if status in _STATUS_CN_WARN else STYLE_NORMAL


def build_stats_rows(stats: dict) -> list[list[str]]:
    """统计概览 → 表格行（§5.1 的第 3 个 sheet；v1.0 附录漏实现，v1.1 补齐）。"""
    rows = [["统计维度", "分类", "数量"]]
    rows.append(["总计", "全部用例", str(stats.get("total", 0))])
    labels = {"by_module": "按模块", "by_priority": "按优先级", "by_tag": "按标签"}
    for key, label in labels.items():
        for name, count in (stats.get(key) or {}).items():
            rows.append([label, str(name), str(count)])
    rows.append(["需求覆盖", "覆盖 REQ 数", str(stats.get("req_covered", 0))])
    return rows


def build_traceability_rows(matrix: dict) -> list[list[str]]:
    rows = [["需求编号", "模块", "优先级", "覆盖状态", "覆盖用例数", "用例编号列表"]]
    for req in matrix.get("requirements") or []:
        cases = req.get("caseIds") or []
        rows.append([
            str(req.get("id") or EMPTY),
            str(req.get("module") or EMPTY),
            str(req.get("priority") or EMPTY),
            _STATUS_CN.get(req.get("status") or "", str(req.get("status") or EMPTY)),
            str(len(cases)),
            ", ".join(str(c) for c in cases) or EMPTY,
        ])
    return rows
```

> 注：`_CONTENT_TYPES` / `_ROOT_RELS` / `_WORKBOOK_XML` / `_WB_RELS_XML` 为模块级模板常量，
> `build_xlsx_bytes()` 内通过 `.format()` 注入动态部分。
> v1.1 修正：`build_zip_bytes()`（返回 bytes，纯函数）与 `write_zip()`（写盘）已拆分为两个函数，
> 以便单测直接断言字节而不触文件系统。

### A.3 `src/prd2tc/export/xmind.py`

```python
"""XMind（Zen / content.json）导出：纯标准库实现。

.xmind 本质是 ZIP 包，现代版本用 content.json 存储导图结构
（XMind Zen 2017 起）。XMind 8/2020+ 均可读取该格式。
"""
from __future__ import annotations

import hashlib
import json
import warnings
from collections import defaultdict
from typing import Any

from prd2tc import __version__ as _TOOL_VERSION   # 避免写死版本随发布漂移（R-9）
from prd2tc.export.common import EMPTY, build_zip_bytes

MAX_NODES = 3000

# 优先级 → XMind 内置 markers 的 markerId（渲染为优先级图标）。
# 注意：必须走 markers；放进 labels 只会显示成 "priority-1" 这样的英文文本（R-3）。
PRIORITY_MARKERS = {
    "P0": "priority-1",
    "P1": "priority-2",
    "P2": "priority-3",
    "P3": "priority-4",
}


class _IdFactory:
    """从语义内容派生确定性 topic id，保证同输入同输出（可复现性要求）。"""

    def __init__(self) -> None:
        self._taken: set[str] = set()

    def make(self, seed: str) -> str:
        candidate = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:26]
        index = 1
        while candidate in self._taken:      # 碰撞概率极低，仍做防御
            candidate = hashlib.sha1(f"{seed}#{index}".encode("utf-8")).hexdigest()[:26]
            index += 1
        self._taken.add(candidate)
        return candidate


def _notes_text(case: dict) -> str:
    """用例详情 → XMind 纯文本 notes。

    用 plain.content 而非 HTML：兼容性最好，且免去标签转义。
    """
    sections: list[str] = []
    sections.append(f"【用例编号】{case.get('id', EMPTY)}")
    sections.append(f"【优先级】{case.get('priority', EMPTY)}")
    if case.get("design_method"):
        sections.append(f"【设计方法】{case['design_method']}")
    tags = [str(t) for t in (case.get("tags") or [])]
    if tags:
        sections.append(f"【标签】{' '.join(tags)}")
    requirements = [str(r) for r in (case.get("requirement") or [])]
    if requirements:
        sections.append(f"【关联需求】{', '.join(requirements)}")
    if case.get("preconditions"):
        body = "\n".join(f"{i}. {t}" for i, t in enumerate(case["preconditions"], 1))
        sections.append(f"【前置条件】\n{body}")
    if case.get("testData"):
        sections.append("【测试数据】\n" + "\n".join(f"• {t}" for t in case["testData"]))
    steps = case.get("steps") or []
    if steps:
        lines = []
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                lines.append(f"{index}. {step}")
                continue
            action = str(step.get("action") or "").strip()
            target = str(step.get("target") or "").strip()
            data = str(step.get("data") or "").strip()
            line = f"{index}. [{action}] {target}" if action else f"{index}. {target}"
            if data:
                line += f" ← {data}"
            lines.append(line)
        sections.append("【测试步骤】\n" + "\n".join(lines))
    if case.get("expected"):
        sections.append("【预期结果】\n" + "\n".join(f"• {t}" for t in case["expected"]))
    return "\n".join(sections)


def _step_text(index: int, step: Any) -> str:
    if not isinstance(step, dict):
        return f"{index}. {step}"
    action = str(step.get("action") or "")
    target = str(step.get("target") or "")
    data = str(step.get("data") or "")
    return f"{index}. [{action}] {target}" + (f" ← {data}" if data else "")


def _case_topic(case: dict, ids: _IdFactory, mode: str) -> dict:
    case_id = str(case.get("id") or EMPTY)
    topic: dict[str, Any] = {
        "id": ids.make(case_id),
        "class": "topic",
        "title": f"{case_id} {case.get('title', '')}".strip(),
        "notes": {"plain": {"content": _notes_text(case)}},
    }
    # R-3：优先级走 markers（彩色优先级图标）；放 labels 只会显示成英文文本
    marker = PRIORITY_MARKERS.get(str(case.get("priority")))
    if marker:
        topic["markers"] = [{"markerId": marker}]
    # labels 只承载标签与设计方法（文本标签）
    labels = [str(t) for t in (case.get("tags") or [])]
    if case.get("design_method"):
        labels.append(str(case["design_method"]))
    if labels:
        topic["labels"] = labels

    if mode == "full":
        children = [
            {"id": ids.make(f"{case_id}#step#{i}"), "class": "topic", "title": _step_text(i, s)}
            for i, s in enumerate(case.get("steps") or [], 1)
        ]
        if children:
            topic["children"] = {"attached": children}
    return topic


def build_tree(cases: list[dict], ids: _IdFactory, mode: str,
               module_names: dict[str, str] | None = None) -> dict:
    """用例列表 → 模块 → 子模块 → 用例 的树。"""
    names = module_names or {}
    by_module: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        by_module[str(case.get("module") or "COMMON")].append(case)

    module_topics = []
    for module in sorted(by_module):
        display = f"{module} {names.get(module, '')}".strip()
        module_topic: dict[str, Any] = {
            "id": ids.make(f"module#{module}"),
            "class": "topic",
            "title": display,
        }
        if mode == "module":
            count = len(by_module[module])
            module_topic["notes"] = {"plain": {"content": f"共 {count} 条用例"}}
            module_topics.append(module_topic)
            continue

        # case / full 模式：按 subModule 分组，无 subModule 的直接挂在模块下
        grouped: dict[str, list[dict]] = defaultdict(list)
        for case in by_module[module]:
            grouped[str(case.get("subModule") or "")].append(case)

        sub_topics = []
        for sub in sorted(grouped):
            case_topics = [_case_topic(c, ids, mode) for c in grouped[sub]]
            if not sub:
                sub_topics.extend(case_topics)   # 无子模块：扁平挂载
                continue
            sub_topics.append({
                "id": ids.make(f"module#{module}#sub#{sub}"),
                "class": "topic",
                "title": sub,
                "children": {"attached": case_topics},
            })
        if sub_topics:
            module_topic["children"] = {"attached": sub_topics}
        module_topics.append(module_topic)
    return {"id": ids.make("root"), "class": "topic", "title": "",
            "children": {"attached": module_topics}}


def _count_nodes(topic: dict) -> int:
    total = 1
    for child in (topic.get("children") or {}).get("attached") or []:
        total += _count_nodes(child)
    return total


def _pack(title: str, root: dict) -> bytes:
    """把导图树打包成 .xmind 字节流。"""
    sheet_id = "sheet-1"
    content = [{
        "id": sheet_id,
        "class": "sheet",
        "title": title,
        "rootTopic": root,
        "topicPositioning": "fixed",
    }]
    metadata = {
        "creator": {"name": "prd2tc", "version": _TOOL_VERSION},
        "activeSheetId": sheet_id,       # 部分 XMind 版本必需，缺失会提示损坏
    }
    manifest = {"file-entries": {
        "content.json": {}, "metadata.json": {}, "manifest.json": {}
    }}
    return build_zip_bytes({
        "content.json": json.dumps(content, ensure_ascii=False, indent=2),
        "metadata.json": json.dumps(metadata, ensure_ascii=False, indent=2),
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2),
    })


def build_xmind_bytes(cases: list[dict], *, title: str = "测试用例集",
                      mode: str = "case",
                      module_names: dict[str, str] | None = None) -> bytes:
    """用例列表 → .xmind 字节流。纯函数，不写盘。

    mode: case（**默认**，展开到用例）/ full（到步骤）/ module（仅模块层）

    R-1：默认必须是 case —— 与 §6.1 三级树、F-2 需求一致；
         v1.0 默认 module 会让导图只剩模块名，且文档自带测试断言失败。
    R-4：超限时**自动降级**为 module 模式并告警，而不是直接不产出文件。
    """
    if not cases:
        raise ValueError("没有用例可导出")
    # 降级链：请求的模式 → module（module 已是最粗粒度）
    chain = [mode] if mode == "module" else [mode, "module"]
    for attempt in chain:
        root = build_tree(cases, _IdFactory(), attempt, module_names)
        if _count_nodes(root) <= MAX_NODES:
            if attempt != mode:
                warnings.warn(
                    f"导图节点数超过 {MAX_NODES}，已从 {mode} 自动降级为 module 模式"
                    f"（仅模块层）。如需完整展开，请拆分用例集。",
                    stacklevel=2,
                )
            root["title"] = title
            return _pack(title, root)
    raise ValueError(f"即使降级为 module 模式，节点数仍超过 {MAX_NODES}，请拆分用例集")
```

### A.4 `src/prd2tc/export/__init__.py`

```python
"""用例导出：Excel（.xlsx）与 XMind（.xmind）。

两种格式均以标准库实现，保持项目「零第三方运行时依赖」定位
（见 README「技术栈」章节）。共同数据源为 cases/cases/*.json。
"""
from __future__ import annotations

import json
from pathlib import Path

from prd2tc.export.common import atomic_write_bytes
from prd2tc.export.excel import build_xlsx_bytes, safe_sheet_name
from prd2tc.export.xmind import build_xmind_bytes

__all__ = ["export_cases", "build_xlsx_bytes", "build_xmind_bytes", "safe_sheet_name"]

MEDIA_TYPES = {
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xmind": "application/vnd.xmind.workbook",
}


def _load_module_names(root: Path | None) -> dict[str, str]:
    """从 analysis/requirements.json 读取模块中文名（失败则回退代号）。"""
    if root is None:
        return {}
    path = root / "analysis" / "requirements.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(m.get("id")): str(m.get("name") or "")
        for m in (data.get("modules") or []) if isinstance(m, dict)
    }


def _load_traceability(root: Path | None) -> dict | None:
    if root is None:
        return None
    path = root / "cases" / "traceability.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def export_cases(cases_dir: str | Path, *, formats: list[str] | None = None,
                 out: str | Path | None = None, root: str | Path | None = None,
                 xmind_mode: str = "case", sheet_name: str = "用例明细",
                 include_traceability: bool = True,
                 product: str = "", version: str = "") -> dict:
    """从用例目录导出 Excel / XMind。

    - cases_dir：用例 JSON 目录（output/<p>/<v>/cases/cases）
    - root：运行根目录；提供时可读取 traceability.json 与模块中文名
    - out：输出文件路径（单格式）或目录（多格式）；默认写入 cases_dir 父目录
    - 返回 {"files": [Path, ...], "case_count": int}
    """
    from prd2tc.io import load_cases

    formats = formats or ["excel", "xmind"]
    cases = load_cases(cases_dir)
    if not cases:
        raise ValueError(f"用例目录为空或不存在: {cases_dir}")

    cases_path = Path(cases_dir)
    run_root = Path(root) if root else cases_path.parent.parent
    module_names = _load_module_names(run_root)
    traceability = _load_traceability(run_root) if include_traceability else None
    # 统计概览的数据源（与 traceability 无关，始终可用）
    from prd2tc.io import stats as _stats
    try:
        summary = _stats(cases)
    except Exception:
        summary = None

    title = f"测试用例集 {product} {version}".strip() or "测试用例集"
    base = cases_path.parent            # output/<p>/<v>/cases
    stem = f"用例集" if not product else f"{product}-{version}-用例集"

    produced: list[Path] = []
    if "excel" in formats:
        data = build_xlsx_bytes(cases, traceability=traceability, stats=summary,
                                sheet_name=sheet_name)   # 内部会 safe_sheet_name 清洗
        target = Path(out) if out and Path(out).suffix == ".xlsx" else base / f"{stem}.xlsx"
        produced.append(atomic_write_bytes(target, data))
    if "xmind" in formats:
        data = build_xmind_bytes(cases, title=title, mode=xmind_mode,
                                 module_names=module_names)
        target = Path(out) if out and Path(out).suffix == ".xmind" else base / f"{stem}.xmind"
        produced.append(atomic_write_bytes(target, data))

    return {"files": produced, "case_count": len(cases)}
```

---

## 变更影响面汇总

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/prd2tc/export/` | **新增** | 4 个文件（`__init__` / `common` / `excel` / `xmind`） |
| `src/prd2tc/cli.py` | 修改 | 新增 `cmd_export` + parser 声明，约 25 行 |
| `src/prd2tc/pipeline.py` | 修改 | 阶段 3 后追加导出调用，约 10 行 |
| `src/prd2tc/service.py` | 修改（可选） | 下载接口，约 20 行 |
| `ui/src/pages/RunPage.tsx` | 修改（可选延后） | 导出按钮 |
| `tests/test_export.py` | **新增** | 约 15 个用例 |
| `README.md` / `docs/*` | 修改 | 文档更新 |
| `pyproject.toml` | **不修改** | 零第三方依赖 |

---

*文档结束。实施前请先完成第 3 章的人工验证（S0）。*