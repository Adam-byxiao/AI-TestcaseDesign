# 用例集导出（Excel / XMind / Markdown）

> 源码：`src/prd2tc/export/{__init__,common,excel,xmind}.py`、`src/prd2tc/nlsteps.py`、
> `src/prd2tc/casefilter.py`、`src/prd2tc/io.py`；集成点 `src/prd2tc/cli.py`、`src/prd2tc/pipeline.py`、
> `src/prd2tc/service.py`、`ui/src`。非显然结论均可在标注行号处复核。

导出层只做一件事：把 `cases/cases/*.json` 这份**机读事实源**渲染成三类**给人评审**的产物，
且不引入第三方运行时依赖（`pyproject.toml:23-25` 的 `service` extra 是可选的 Web 依赖，
导出路径不依赖其中任何一个）。「单一数据源」是硬约束：三种产物互不读取，尤其**不读 Markdown**，
避免出现两份会分叉的事实（`export/__init__.py:1-5`）。

## 1. 产物清单与共同入口

| 产物 | 生成者 | 数据源 | 触发 |
|---|---|---|---|
| `cases/用例集.md` | `io.render_cases_md()`（`io.py:184`） | 内存中的 `cases` | 阶段 3 自动写（`io.py:17` 的 `OUT_STAGES`） |
| `cases/<product>-<version>-用例集.xlsx` | `excel.build_xlsx_bytes()`（`excel.py:286`） | `cases/cases/*.json` + `cases/traceability.json` | `generate` / `prd2tc export` / 服务端点 |
| `cases/<product>-<version>-用例集.xmind` | `xmind.build_xmind_bytes()`（`xmind.py:190`） | 同上 + `analysis/requirements.json`（模块中文名） | 同上 |

两种二进制格式的公开契约是**纯函数** `build_*_bytes(...) -> bytes`，不碰文件系统；
写盘由调用方经 `common.atomic_write_bytes()` 完成（`common.py:58`）。格式逻辑因此可用内存
断言测试，写盘只需一条集成测试。`export_cases()`（`export/__init__.py:68`）串起全部步骤：
`load_cases` 读取用例目录（空 → `ValueError`，`:89-90`）；`casefilter.apply` 做纯筛选并保持
原顺序（筛选后为空 → `ValueError`，`:93-98`）；`filtered` 时按子集重算追踪矩阵，否则沿用磁盘
矩阵（`:103-112`）；`io.stats` 产出统计摘要并在筛选时补 `scope`（`:114-122`）；文件名 stem 由
产品/版本/筛选片段拼出（`:128-131`）；最后两个 `build_*_bytes` 经 `atomic_write_bytes` 落盘
（`:134-143`）。返回固定为
`{"files":[Path,…], "case_count", "total_case_count", "filter"}`（`:145-147`），`filter` 来自
`casefilter.summarize()`，供调用方回显「共 3 条，命中 2 条，排除 1 条」。
**为什么入口承担「重算」而非只做「渲染」**：若各调用方自行决定统计与追踪矩阵的口径，
迟早出现「Excel 里 4 条用例、统计页写 100 条」这类自相矛盾；口径收敛到唯一入口，
是本文件所有一致性保证的前提（§6.4）。

## 2. Excel（.xlsx）：手写 OOXML 子集

### 2.1 它就是一个 ZIP 容器

`.xlsx` = ZIP + 若干 XML part，导出只需 OOXML 的**子集**：无公式、图表、共享字符串表、数据透视，
因此 `zipfile` + `xml.sax.saxutils.escape` 足够（`excel.py:1-6`）。

| ZIP 成员 | 作用 | 生成处 |
|---|---|---|
| `[Content_Types].xml` | 声明各 part 的 MIME：`Default` 按扩展名（`rels`/`xml`），`Override` 按路径（workbook、每个 worksheet、styles） | `excel.py:72-78` |
| `_rels/.rels` | 包级关系：`rId1` → `officeDocument` → `xl/workbook.xml` | `excel.py:80-83` |
| `xl/workbook.xml` | 工作簿 `<sheets>` 列表（`name` / `sheetId` / `r:id`） | `excel.py:85-89` |
| `xl/_rels/workbook.xml.rels` | 工作簿级关系：每 sheet 一条 `worksheet`，末尾一条 `styles` | `excel.py:91-94` |
| `xl/styles.xml` | 字体 / 填充 / 边框 / 单元格格式 | `excel.py:100-127` |
| `xl/worksheets/sheetN.xml` | 工作表数据（全部 `inlineStr`） | `excel.py:147-180` |

`[Content_Types].xml` **不声明自己**（规范如此），`.rels` 由 `Default Extension="rels"` 覆盖；
其余每个 `.xml` part 都必须有声明（`test_excel_content_types_cover_all_parts`，`tests/test_export.py:145`）。

### 2.2 relationship 的 `Type` URI 必须带 `/2006/`（最高风险项）

`Relationship@Type` 是**完整 URI**，漏掉中间的 `/2006/`，Excel 直接判定文件损坏：

```xml
<!-- ✗ 错误：Excel 弹「格式警告」；点「修复」后只剩工作表分页，没有任何内容 -->
<Relationship Id="rId1" Target="worksheets/sheet1.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/relationships/worksheet"/>
<!-- ✓ 正确 -->
<Relationship Id="rId1" Target="worksheets/sheet1.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
```

三个合法 URI（`tests/test_export.py:109-113` 逐个断言）：
`http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument`、
`…/2006/relationships/worksheet`、`…/2006/relationships/styles`。

**根因与防复发**：该 URI 最初在 f-string 里跨行拼接，拆行时丢掉了 `2006/`。现在它是模块级
常量 `REL_NS`（`excel.py:34`），关系条目统一单行插值（`excel.py:315-321`），从结构上避免再次拆错。
**为什么一条 URI 能骗过所有自检却骗不过 Excel**：`openpyxl` 是**宽容解析器**，既不校验关系 URI
合法性，也不严格校验元素顺序，因此当年「openpyxl 交叉验证通过」是误判。

> **验证纪律**：xlsx 兼容性的**权威判据是真实 Excel 打开文件**，不是 openpyxl。互补手段是补上
> openpyxl 看不见的结构断言：`test_excel_relationship_types_are_valid`（`tests/test_export.py:99`）、
> `test_excel_relationship_targets_exist`（`:132`，`Target` 必须真实存在于 ZIP 内）、
> `test_excel_content_types_cover_all_parts`（`:145`）。

### 2.3 `workbook.xml` 与 sheet 命名

工作簿按 sheet 顺序生成 `rId1..rIdN`，样式表固定占 `rId(N+1)`（`excel.py:310-321`）。
命名有三重独立约束：**非法字符清洗**——Excel 禁止 `: \ / ? * [ ]`，`safe_sheet_name()`
（`excel.py:57-64`）替换为 `-` 并 `strip()`，空串回退 `sheet`；**31 字符上限**——同一函数截断；
**唯一性**——Excel 要求工作簿内 sheet 名唯一，重名即判损坏，`_dedupe_sheet_names()`
（`excel.py:266-283`）撞名时追加 `-2`、`-3`…，并用 `base[:28]` 给后缀留空间以保证去重后仍 ≤31
字符。现实触发场景：用户 `--sheet-name 统计概览` 撞上内置页签名
（`test_excel_sheets_deduplicated_when_name_collides`，`tests/test_export.py:454`）。

另有一条易混淆的转义规则：**属性值与元素正文要求不同**。正文只需转义 `& < >`（`escape()`
默认行为，`excel.py:143`），双引号在正文里合法；sheet 名写在属性里，必须额外转义 `"`
——这正是 `_attr()` 的职责（`excel.py:67-69`）。`safe_sheet_name()` 只管 Excel 命名限制，
`_attr()` 只管 XML 良构性，两者缺一不可（`excel.py:59-62`；
`test_sheet_name_special_chars_are_escaped`，`tests/test_export.py:257`）。

### 2.4 样式表 `xl/styles.xml`

元素顺序由 OOXML 规定，写错同样被判损坏：
`fonts → fills → borders → cellStyleXfs → cellXfs → cellStyles → dxfs → tableStyles`。
`cellStyles` 极易漏掉且排在 `cellXfs` **之后**；缺它时 openpyxl 报
`Workbook contains no default style`，严格实现可能直接失败（`test_excel_has_default_style`，
`tests/test_export.py:228`）。实际计数：fonts=3、fills=5、borders=1、cellStyleXfs=1、cellXfs=6、
cellStyles=1、dxfs=0、tableStyles=0（`excel.py:100-127`）。填充色：`P0_FILL=FFFFC7CE`（浅红）、
`P1_FILL=FFFFEB9C`（浅黄）、`WARN_FILL=FFFFE1E1`（覆盖不足浅红），P0 字体色 `FF9C0006`
（深红加粗，`excel.py:105`）。`cellXfs` 的 6 个条目与 Python 常量**按序一一对应**（`excel.py:19-24`）：

| s | 常量 | 字体 | 填充 | 对齐 |
|---|---|---|---|---|
| 0 | `STYLE_NORMAL` | 普通 | 无 | `wrapText=1 vertical=top` |
| 1 | `STYLE_HEADER` | 加粗 | 无 | 居中（表头不需换行） |
| 2 | `STYLE_P0` | 加粗深红 | P0 浅红 | `wrapText=1` |
| 3 | `STYLE_P1` | 普通 | P1 浅黄 | `wrapText=1` |
| 4 | `STYLE_WARN` | 普通 | 警示浅红 | `wrapText=1` |
| 5 | `STYLE_WRAP` | 普通 | 无 | `wrapText=1`（同 s=0，留独立索引便于扩展） |

> **每个强调样式都自带 `wrapText`，不依赖回退**（`excel.py:98-99`）：若把「底色」与「自动换行」
> 做成互斥的单一样式，P0/P1 行虽着色但 `wrapText` 丢失，「测试步骤」「预期结果」挤成一行，
> 而 P0 恰恰最需要审阅。`test_excel_p0_row_keeps_wrap_text`（`tests/test_export.py:208`）取出
> 第 2 行**实际引用的样式索引**再查 `cellXfs` 含 `wrapText`，锁行为而非常量。同理 `STYLE_WARN`
> 必须真被引用：`_coverage_style()`（`excel.py:225-228`）给「覆盖不足 / 未覆盖」行染浅红（`:238`）。

### 2.5 工作表结构：元素顺序即正确性

`_sheet_xml()`（`excel.py:147-180`）严格按 `sheetViews → cols → sheetData → autoFilter` 书写：

```xml
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2"
      activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>     <!-- 冻结表头 -->
  <cols><col min="1" max="1" width="18" customWidth="1"/>…</cols>           <!-- 须在 sheetData 前 -->
  <sheetData><row r="1"><c r="A1" s="1" t="inlineStr"><is><t xml:space="preserve">用例编号</t></is></c>…</row>…</sheetData>
  <autoFilter ref="A1:M22"/>                                                <!-- 须在 sheetData 后 -->
</worksheet>
```

| 特性 | 实现 |
|---|---|
| 冻结表头 | `pane ySplit="1" topLeftCell="A2" state="frozen"` |
| 自动筛选 | `<autoFilter ref="A1:{末列}{末行}"/>`，置于 `sheetData` **之后** |
| 列宽 | `<cols><col min/max/width/customWidth/></cols>`，置于 `sheetData` **之前** |
| 自动换行 | 单元格 `s` 指向自带 `wrapText` 的 `cellXfs` 条目 |
| 字符串 | `t="inlineStr"` + `<is><t xml:space="preserve">`（不建共享字符串表） |

冻结与自动筛选只加在**第一个 sheet**（`freeze=(i==1)`、`autofilter=(i==1)`，`excel.py:336-340`）：
统计页与矩阵页列数不同，筛选器意义不大，冻结反而占视觉空间。`col_name()`（`excel.py:130-137`）
实现 0→A、25→Z、26→AA、702→AAA；当前最多 M 列，但实现不设上限。

### 2.6 三个 sheet

**sheet 1「用例明细」**（`--sheet-name` 可改名）：列宽取自 `COLUMNS`（`excel.py:36-51`），
行样式由 `_priority_style()` 按优先级着色（`excel.py:210-216`）。

| 列 | 宽 | 字段 | 渲染 |
|---|---|---|---|
| 用例编号 / 用例标题 | 18 / 40 | `id` / `title` | 原文 |
| 模块 / 子模块 / 状态 | 12 / 12 / 12 | `module` / `subModule` / `status` | 空 → `—`；状态空 → `active` |
| 设计方法 / 优先级 | 14 / 8 | `design_method` / `priority` | 空 → `—`；P0/P1 决定整行着色 |
| 标签 / 关联需求 | 20 / 24 | `tags[]` / `requirement[]` | 空格连接 / 逗号连接 |
| 前置条件 / 测试数据 / 预期结果 | 32 / 24 / 40 | `preconditions[]` / `testData[]` / `expected[]` | 换行连接 + `strip_locators` |
| 测试步骤 | 52 | `steps[]` | **自然语言**（§5） |

占位符 `common.EMPTY = "—"`（`common.py:21`）：空单元格无法与「忘了填」区分，显式占位让
「确实没有」也成为可读信息。**sheet 2「需求覆盖」**（列宽 `[16,12,8,12,10,40]`）由
`build_traceability_rows()`（`excel.py:231-244`）铺成
`需求编号 / 模块 / 优先级 / 覆盖状态 / 覆盖用例数 / 用例编号列表`；状态经 `_STATUS_CN` 中文化
（`covered/insufficient/uncovered/excluded` → 已覆盖/覆盖不足/未覆盖/已排除，`excel.py:219-220`），
只有「覆盖不足」「未覆盖」高亮（`_STATUS_CN_WARN`，`excel.py:222`）——评审要盯的是缺口。
**sheet 3「统计概览」**（列宽 `[16,20,10]`）由 `build_stats_rows()`（`excel.py:247-263`）输出
「统计维度 / 分类 / 数量」，内容为 `总计·全部用例`、按模块、按优先级、按标签、
`需求覆盖·覆盖 REQ 数`；筛选导出时首行插入「导出范围」（§6.5）。统计计算被包在 `try/except`
（`__init__.py:115-118`）里：统计失败只少一个 sheet，不会让导出失败——统计是**附加信息**。

### 2.7 单 sheet 行数上限

`EXCEL_MAX_ROWS = 1_048_576`（`excel.py:16`）；`len(cases) + 1 > EXCEL_MAX_ROWS`（`:296-297`，
+1 为表头行）时抛 `ValueError`，含实际条数与上限。用**显式失败**而非静默截断：截断后的工作簿
看起来完全正常，足以让缺失的用例流到交付。当前规模（单 PRD 通常 < 2000 条）远未触及，
但失败路径必须确定。分表（每模块一 sheet）架构上已支持——`<sheets>` 与 `sheetN.xml` 都是
循环生成的（`excel.py:310-340`）。

## 3. XMind（.xmind）：JSON 版导图

### 3.1 包与成员

XMind Zen（2017）起用 `content.json` 存结构，当前版本仍能读该格式，兼容面最广（`xmind.py:1-5`）。
`_pack()`（`:166-187`）经 `build_zip_bytes()` 写出三个成员：`content.json`（单元素数组
`{id:"sheet-1", class:"sheet", title, rootTopic, topicPositioning:"fixed"}`）、`metadata.json`
（`{"creator":{"name":"prd2tc","version":<prd2tc.__version__>}, "activeSheetId":"sheet-1"}`）、
`manifest.json`（`{"file-entries":{"content.json":{},"metadata.json":{},"manifest.json":{}}}`）。
`activeSheetId` 标为「部分 XMind 版本必需，缺失会提示损坏」（`:178`），是成本极低的兼容性保险；
`creator.version` 取 `prd2tc.__version__`（`:14`）而非写死，避免版本漂移；**不生成缩略图**
——`manifest.json` 只列实际写出的成员，避免「声明了但不存在」的成员。

### 3.2 三级树

`build_tree()`（`xmind.py:115-156`）组织为 **rootTopic → 模块 → 子模块 → 用例**：root 下是模块，
模块下是子模块，子模块下是用例；`subModule` 为空时用例扁平挂到模块下（`:143-145`），省掉无信息的
中间层。三点实现细节：**模块名映射**——`_load_module_names()`（`__init__.py:25-39`）读
`analysis/requirements.json` 的 `modules[].name`（如 `KB 知识库`），文件缺失或解析失败时回退纯代号；
**缺模块名兜底**——`case["module"] or "COMMON"`（`xmind.py:121`），不会有节点直接挂根上；
**确定性排序**——模块 `sorted(by_module)`、子模块 `sorted(grouped)`（`:124`、`:141`），同一子模块内
保持输入顺序（`load_cases` 按文件名排序读取，`io.py:134`），排序让布局稳定。

### 3.3 三种 `mode` 与节点数降级

`build_xmind_bytes(mode=...)`（`xmind.py:190-215`）默认 `case`：

| mode | 展开到 | 节点数 | 适用 |
|---|---|---|---|
| `module` | 仅模块层；模块节点带备注「共 N 条用例」 | root + 模块 | 超大规模用例集的兜底 |
| `case`（默认） | 模块 → 子模块 → 用例 | root + 模块 + 子模块 + 用例 | 日常评审 |
| `full` | 再把每步展开为用例的子节点 | 上一行 + 步骤总数 | 需在导图里逐步走查 |

`MAX_NODES = 3000`（`xmind.py:18`）。超限时**自动降级为 `module` 并告警**，不直接失败
（`xmind.py:203-215`）：

```python
chain = [mode] if mode == "module" else [mode, "module"]
for attempt in chain:
    root = build_tree(cases, _IdFactory(), attempt, module_names)
    if _count_nodes(root) <= MAX_NODES:
        if attempt != mode:
            warnings.warn(f"导图节点数超过 {MAX_NODES}，已从 {mode} 自动降级为 module 模式…")
        root["title"] = title
        return _pack(title, root)
raise ValueError(f"即使降级为 module 模式，节点数仍超过 {MAX_NODES}，请拆分用例集")
```

**为什么降级而非报错**：导图是评审辅助物，「粒度变粗」远好于「没有产物」。`case` 模式节点数
≈ 用例数 + 模块数 + 子模块数，3000 节点对千条量级工具并不罕见（测试
`test_xmind_degrades_instead_of_raising`，`tests/test_export.py:358` 用 `MAX_NODES` 条用例构造
必然降级，断言「仍产出文件 + 发出告警」）。末尾保留 `raise` 作最后防线：只有 `module` 模式也
超限（模块数 > 3000，实际不可能）才失败。降级走 `warnings.warn` 而非返回值上报，是为了让 CLI、
服务日志、流水线**所有调用方**看到同一信号，无需各自实现「是否降级」的协议。

### 3.4 节点承载与稳定 id

节点标题是 `"{id} {title}"`，`class` 固定 `"topic"`（`xmind.py:86-93`）；信息分工如下：

| 信息 | 字段 | 渲染效果 |
|---|---|---|
| 优先级 | `markers`（`PRIORITY_MARKERS`，`xmind.py:22-27`） | 彩色优先级图标（P0→`priority-1` … P3→`priority-4`） |
| 标签 / 设计方法 | `labels` | 文本标签 |
| 用例详情 | `notes.plain.content` | 右侧备注面板（纯文本） |

**优先级必须走 `markers`**：`labels` 是纯文本标签，把 `priority-1` 放进去，XMind 只会原样显示
这串英文，不会渲染成图标（`test_xmind_markers_include_priority`，`tests/test_export.py:325`）。
`notes` 用 `plain.content` 而非 HTML：兼容性最好，且免去标签转义（`xmind.py:46-49`）。
`_notes_text()`（`:46-72`）按固定顺序拼装可缺省段落：`【用例编号】【优先级】【设计方法】【标签】
【关联需求】【前置条件】【测试数据】【测试步骤】【预期结果】`。`full` 模式下每步额外成为子节点，
标题由 `_step_text()`（`:75-83`）生成 `"{序号}. {step_sentence(step)}"`——导图节点宽度有限，
机器标记在这里尤其碍眼。

topic id 由 `_IdFactory`（`:30-43`）从**语义内容**派生：sha1 前 26 位，碰撞时以 `f"{seed}#{index}"`
重试；种子分别为根 `"root"`、模块 `f"module#{module}"`、子模块 `f"module#{module}#sub#{sub}"`、
用例为其 `id`、`full` 步骤为 `f"{case_id}#step#{i}"`。不用 `uuid4()` 是为了字节一致性（§7）；
用哈希而非直接拿用例 id 当 topic id，是因为用例 id 里的 `#`、中文、空格都不是 XMind 预期的
id 形态。

## 4. Markdown 评审稿

`render_cases_md()`（`io.py:184-254`）由 `write_stage()` 写到 `cases/用例集.md`（文件名来自
`io.py:17`）。固定结构：标题 + 「机读源为 `cases/cases/*.json`」声明；**§1 统计概览**
（总数、按优先级、按模块、覆盖 REQ 数，来自 `io.stats()`）；**§2 需求回溯表**（两列表格
`REQ | 覆盖用例`，按 REQ 编号排序，`io.py:199-209`）；**§3 用例明细（按模块）**（按模块分节，
`用例 / 标题 / 优先级 / 标签` 四列表格）；**§4 用例步骤（自然语言）**（逐条展开关联需求、
优先级、设计方法、前置条件、测试数据、步骤、预期结果）。两个易错点：**步骤不得双重编号**
——`render_steps_nl()` 自带 `1. 2. …`，第 4 章因此用 `numbered=False` 输出裸句子再逐行加 `- `
（`io.py:243-245`；`test_markdown_steps_not_double_numbered`，`tests/test_nl_steps.py:253`）；
**自由文本字段同样清洗**（前置条件、测试数据、预期结果都过 `clean_items()`）。
Markdown 与 Excel/XMind 共享同一组渲染函数（§5），因此三种产物对同一段步骤的表述必然一致。

## 5. 自然语言步骤渲染（`nlsteps.py`）

### 5.1 为什么导出物用自然语言，而 JSON 保留结构化

结构化步骤（`{"action":"goto","target":"智能体配置页 (page=config)","data":""}`）是**给机器看的**：
动作枚举 + 契约定位符，门禁据此校验、自动化据此驱动；直接放进 Excel / XMind / Markdown 时，
`[goto]`、`(page=config)` 严重妨碍阅读。约定（`nlsteps.py:1-10`）：`cases/cases/*.json` 保留结构化
（消费者是门禁、自动化、`--repro`）；导出物使用自然语言（消费者是产品 / 测试评审）。
`common.render_steps(structured=False)`（`common.py:30-55`）默认走自然语言，`structured=True`
可切回 `1. [action] target ← data` 供排障对照。结构化原文从不被覆盖：
`test_structured_json_keeps_machine_format`（`tests/test_nl_steps.py:286`）断言落盘 JSON 里
`steps[0]` 仍是原样的 dict。

### 5.2 为什么用确定性渲染，而不是让模型另写一套描述

三条理由（`nlsteps.py:12-18`）：`target` 里**本就带中文元素名**
（`智能体配置页 (page=config)`），动词映射即可成句，无需额外推理；**不会与结构化步骤矛盾**
——若由模型另写一套，两份描述可能分叉，而结构化那份才是门禁与执行的依据（两份描述一旦分叉
就无法追责）；零 token 成本、零非确定性，`--repro` 下逐字可复现。模型的唯一职责是产出结构化
步骤；「说人话」是**纯函数**的职责。

### 5.3 target 拆分与动作映射

`split_target()`（`nlsteps.py:63-86`）拆出 `(元素名, 种类, 取值)`，同时支持模型实测会出现的两种
写法：`智能体配置页 (page=config)` → `('智能体配置页','page','config')`（`_ANNOT_RE`）；
`知识库配置页 page=knowledge` → `('知识库配置页','page','knowledge')`（`_BARE_ANNOT_RE`）；
`工具列表与详情区` → `('工具列表与详情区','','')`。无括号变体是必需的：只处理括号版时导出物会
残留 `打开「知识库配置页 page=knowledge」`
（`test_split_target_handles_bare_annotation_without_parens`，`tests/test_nl_steps.py:35`）。
动作映射分两组：`_VERBS`（`nlsteps.py:33-46`：`goto→打开{t}`、`click→点击{t}`、
`fill→在{t}输入「{d}」`、`selectOption/check/uncheck/hover/press/reload/upload/scrollTo/waitFor`）
与 `_ASSERTIONS`（`:47-60`：`expectVisible/expectHidden/expectText/expectValue/expectEnabled/
expectDisabled/expectState/expectCount/expectUrl/expectChecked/expectUnchecked`）。
`step_sentence()`（`:89-111`）的四个刻意行为：断言类统一加**「验证」前缀**（`:109-110`，
便于一眼区分「操作」与「检查点」）；无 `data` 时从模板删掉 `{d}` 及引号（`:97-99`，
避免 `输入「」`）；元素名为空时用 `（{kind}={value}）` 或「当前页面」，不产出 `打开「」`；
未知动作回退为 `{action}「{name}」：{data}`，**不静默丢信息**。

### 5.4 `strip_locators`：只剥离「种类=取值」标注

模型有时把定位符写进自由文本，例如 `已进入智能体配置页（page=config）`、
`页签栏可见（class=tabs）`（实测在一份产物中出现 68 处），在评审列里同样碍眼；因此导出的
preconditions / testData / expected 都要过 `strip_locators()`（`nlsteps.py:145-159`）。
**关键约束：只剥离带 `种类=` 的标注，裸括号是正常说明，不得误删。** 实现靠
`_LOCATOR_IN_PROSE_RE`（`:138-140`）显式枚举定位符种类：

```python
r"[（(]\s*(?:data-testid|id|class|page|tab|text|name|label|placeholder)\s*=\s*[^()（）]*[)）]"
```

| 输入 | 输出 |
|---|---|
| `已进入智能体配置页（page=config）` / `点击按钮 (class=primary) 后提交` | `已进入智能体配置页` / `点击按钮 后提交` |
| `系统存在账号（正常状态）`、`支持 txt、docx、xlsx（不含 pdf）`、`（page=config）` | **原样保留**（裸括号；末例因剥离后为空而回退原文，不产出空字段） |

后续还清掉被掏空的括号（`_EMPTY_PAREN_RE`）、压缩双空格、去掉尾随中文标点。
**作用范围限于导出渲染**，JSON 原文一字不改。`clean_items()`（`:162-164`）是「过滤空串 +
剥离定位符」的组合，被 `io.render_cases_md`、`excel._case_row`、`xmind._notes_text` 共用，
因此三份产物的口径一致（测试 `tests/test_nl_steps.py:220-244`）。

### 5.5 四个渲染入口

`step_sentence(step)` → 单句（XMind `full` 步骤节点标题）；`render_steps_nl(steps, numbered=True)`
→ 行列表（Markdown 第 4 章用 `numbered=False`、XMind notes）；`render_steps_nl_text(steps, sep="\n")`
→ 单块文本（Excel「测试步骤」单元格，经 `common.render_steps`）；`render_steps_nl_inline(steps)`
→ `；`连接的单行、句末加 `。`（预留）。

## 6. 筛选导出

### 6.1 语义：维度内取并集，跨维度取交集

`casefilter` 支持四个条件：`modules`、`priorities`、`tags`、`keyword`（关键词匹配用例 `id` /
`title` / 关联需求编号，`casefilter.py:110-129`）。唯一规则（`casefilter.py:3-13`）：
`modules=["KB","AGENT"]` → KB 或 AGENT；`modules=["KB"], priorities=["P0"]` → KB 且 P0；
`modules=["KB"], tags=["@smoke","@regression"]` → KB 且（含 @smoke 或 @regression）。
**为什么维度内是 OR**：选多个模块的语义就是「这几个模块都要」；若维度内也做 AND，多选模块永远
选不出任何用例——那是个无用的默认。而「指定单模块中的回归用例」是跨维度组合（AND），
由 `matches()` 逐维度短路实现。`CaseFilter.active`（`:42-45`）判定「是否真的在筛选」：
空条件 + 空白关键词 = 不筛选，因此必须走与「不传筛选」完全相同的路径
（`test_unfiltered_export_behavior_unchanged`，`tests/test_export.py:619`）。

### 6.2 典型用例

| 诉求 | 调用 | 命中（3 条示例数据） |
|---|---|---|
| 只导出某模块 / 只导出 P0 | `make_filter(modules=["KB"])` / `make_filter(priorities=["P0"])` | 2/3 |
| 只导出 smoke | `make_filter(tags=["@smoke"])` | 1/3 |
| **只导出指定单模块中的回归用例** | `make_filter(modules=["KB"], tags=["@regression"])` | 2/3 |
| 只看某需求的用例 | `make_filter(keyword="REQ-KB-001")` | 命中该需求相关用例 |

后四种组合各有测试（`tests/test_export.py:531-556`），其中「单模块 + 标签」是**用户提出的原始
诉求**，也是选择 OR/AND 语义的直接依据。筛选对调用方透明：`case_count` 是命中数、
`total_case_count` 是全量数、`filter.excluded` 是排除数（`__init__.py:145-147`）。
`test_filtered_export_only_contains_matching_cases`（`tests/test_export.py:513`）直接解压工作簿
XML，断言未命中的用例编号**不得出现**在文件里。

### 6.3 `facets` 分面计数：统计某维度时必须忽略它自身的约束

`facets()`（`casefilter.py:160-215`）返回各维度可选值及**当前条件下的实时数量**。核心是
`_reduced(spec, dimension)`（`:148-157`）：去掉指定维度自身的约束、保留其它维度与关键词。
`ignore` 取具体维度名时只忽略该维度自身；取 `"*"` 时**每个维度各自忽略自身**（分面搜索的
标准语义）；为空则全部维度生效（候选池 = 命中集）。**为什么必须忽略自身**：选了「模块=KB」后，若优先级列表仍受模块约束，优先级候选只剩 KB 里的项
——数字**塌缩成已选项**，用户无法比较「换成 P1 还剩多少条」，复合选择退化为盲选。`"*"` 是 UI 实际
使用的模式（`ui/src/components/CaseFilterPanel.tsx:39-42`），它不只是语法糖：逐维度发三次请求再
合并，三个响应会互相覆盖，计数随机取决于哪次最后到达（前端注释记录了该实测）。实测（3 条用例：
KB/P0/@smoke+@regression、KB/P1/@regression、AGENT/P0/@regression；条件
`modules=["KB"], priorities=["P0"]`，命中 1 条）：

```
ignore="*"   → modules: AGENT=1, KB=1   priorities: P0=1, P1=1   tags: @regression=1, @smoke=1
ignore=""    → priorities: P0=1          # 塌缩：只剩已选项
```

排序同样确定并与 UI 语义对齐：模块按名称升序、优先级按 `P0>P1>P2>P3`（未知值最后，
`_PRIORITY_ORDER`，`:27`）、标签按数量降序、同数量按名称升序（`:204-215`）；`counts()` 跳过
空值（`:190-196`），缺模块名/优先级的用例不会在分面里造出空串选项。

### 6.4 筛选导出必须按子集**重算**追踪矩阵

```python
filtered = bool(spec is not None and spec.active)                     # __init__.py:103
if filtered:
    requirements = _load_requirements(run_root)
    traceability = build_traceability(requirements, cases) if requirements else None
else:
    traceability = _load_traceability(run_root) if include_traceability else None
```

**若沿用磁盘上的全量矩阵**：Excel「需求覆盖」页会声称覆盖了一批**根本不在这个文件里**的用例。
统计页说 4 条、覆盖页列出 20 个用例编号——两个 sheet 自相矛盾，且这种矛盾不会被任何人发现
（两边看起来都很「正常」）。重算后的语义是明确的：`build_traceability()`（`traceability.py:5-46`）
遍历**全部需求**，用子集里的用例数决定状态——筛掉的模块，其需求在文件里显示为「未覆盖」。
这正是应该看到的结果：对一份只含 AGENT 用例的文件，「KB 需求未覆盖」不是数据错误，
而是文件范围的正确表述。`test_filtered_export_recomputes_traceability`
（`tests/test_export.py:559`）专门构造能**区分重算与沿用**的场景：磁盘上放一份谎称「全部已覆盖」
的矩阵，断言导出物里筛掉的 `REQ-KB-001` 显示为「未覆盖」——仅断言「需求行存在」不够，
全量矩阵里也有这些行。**边界**：`analysis/requirements.json` 缺失或为空时无法重算，此时
`traceability = None`，筛选导出**不做**「需求覆盖」sheet——宁愿少一页，也不放一页错的。

### 6.5 统计页标注「导出范围」与文件名带筛选片段

筛选导出时 `summary["scope"]`（`__init__.py:119-122`）写成
`筛选：模块=KB（2/3 条，已排除 1 条）`，由 `build_stats_rows()` 置于统计页**首行**
（`excel.py:253-256`），列名「导出范围」。**为什么放首行**：读者会先看到「总计 2」，若无任何
限定必然把 2 当成全量结论；把范围写在总数之前是最低成本的防误读手段。不带筛选时不出现该行
（避免噪声，`tests/test_export.py:591-629`）。同样的标注进入导图标题：`title` 追加
`（{spec.describe()}）`（`__init__.py:127`），XMind 页签即可看出是子集
（`test_filtered_xmind_title_and_filename_reflect_filter`，`tests/test_export.py:632`）。

`CaseFilter.name_suffix()`（`casefilter.py:60-76`）把条件压成文件名片段：
`modules=["KB"]+priorities=["P0"]` → `KB-P0`；`tags=["@smoke"]` → `smoke`（剥 `@` 前缀）；
`keyword` → 原词。清洗规则：`[<>:"/\\|?*\x00-\x1f]` → `_`（`_UNSAFE_IN_NAME`），`strip(" .-_")`
去首尾，截断 60 字符；`@` **保留在 `describe()` 里**（`标签=@smoke` 比 `smoke` 更明确），
只在文件名里剥掉。拼接顺序（`__init__.py:128-131`）：`stem = "用例集" |
"<product>-<version>-用例集"`，有筛选则 `f"{stem}-{suffix}"`，写到 `cases/<stem>.xlsx|.xmind`。
**为什么文件名必须带筛选片段**：不同筛选各自产出文件，若都叫 `用例集.xlsx`，后一次会**静默覆盖**
前一次（写盘走 `os.replace`，`common.py:72-79`），用户可能拿着「P0 子集」当「全量」用；片段化后
`P-v1-用例集-KB-P0.xlsx` 与 `P-v1-用例集-smoke.xlsx` 并存，也自带出处说明。`out` 显式给出且
扩展名匹配时以它为准（`:137`、`:142`），否则回退 `cases/`。

### 6.6 筛选为空必须显式失败

```python
cases = casefilter.apply(all_cases, spec)
if not cases:
    raise ValueError(
        f"筛选后没有匹配的用例（共 {len(all_cases)} 条，条件：{spec.describe() if spec else '无'}）；"
        f"请放宽条件后重试")                                   # __init__.py:92-98
```

**静默产出空文件是最坏的失败方式**：文件能打开、大小正常（ZIP 头 + 空表头），用户以为导出成功，
直到评审才发现没有内容——而那时筛选条件早已忘记。显式失败还把「共有多少条」「用了什么条件」
写进错误信息（`test_empty_filter_result_fails_loudly`，`tests/test_export.py:603`）。
三种「空」的区分：用例目录为空/不存在 → `ValueError("用例目录为空或不存在: …")`（`:89-90`）；
全量非空、筛选后为空 → 上例；空列表直接传给 `build_*_bytes` → `ValueError("没有用例可导出")`
（`excel.py:294`、`xmind.py:200`，纯函数的自防御，即使绕开 `export_cases` 也造不出空产物）。

### 6.7 筛选能力的入口覆盖

| 入口 | 支持筛选 | 说明 |
|---|---|---|
| Python API `export_cases(spec=…)` | ✅ | 一切筛选能力的源头 |
| 服务端点 `GET /api/v1/exports/{run_id}/{fmt}` | ✅ | 查询参数 `modules`/`priorities`/`tags`/`keyword`（重复键，`service.py:944-973`） |
| 前端「导出」对话框 | ✅ | 复用运行页的批量选择（`ExportDialog.tsx:59-62`） |
| CLI `prd2tc export` | ❌ **当前无筛选参数** | parser 只有 `--cases/--format/--out/--product/--version/--xmind-mode/--sheet-name/--no-traceability`（`cli.py:296-305`） |

CLI 需要筛选时目前只能走 Python API 或服务端点。这是**已知缺口**而非设计意图：补起来只需给
parser 加 `--modules/--priorities/--tags` 并在 `cmd_export` 里构造 `spec`（`cli.py:197-207`
已传入 `root=`）。

## 7. 确定性：同输入产出字节一致

三个必要条件（`common.py:89-98`）：ZIP 时间戳固定为
`FIXED_TIMESTAMP = (2020,1,1,0,0,0)`（`common.py:19`），ZIP 头里没有 `now()`；成员按名称
`sorted()` 遍历，并固定 `compress_type` 与 `external_attr`（`0o600 << 16`），目录项顺序与属性
全部确定；XMind 的 topic id 由语义内容派生（§3.4），不含 uuid4 与时间。

```python
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
```

构造顺序也确定：`members` 是字面量 dict，`sheetN.xml` 按序号写入（`excel.py:336-340`）；
XMind 的 `json.dumps(..., ensure_ascii=False, indent=2)`（`xmind.py:184-186`）保持插入序。
守护测试：`test_excel_is_deterministic`（`tests/test_export.py:194`）、
`test_xmind_is_deterministic`（`:342`）、`test_filtered_export_is_deterministic`（`:648`）。
本次复核：`pytest -q tests/test_export.py tests/test_nl_steps.py` → **69 passed in 1.03s**
（Python 3.12.10 / Windows AMD64）；同一输入在**三个独立进程**中导出，xlsx 与 xmind 的 sha256
完全一致（xlsx `86fd5c94…`、xmind `435e6f22…`），说明确定性不依赖进程内状态。

**对 `compare-runs` 的意义**需精确区分两件事：其一，导出产物**会进入产物清单**——
`pipeline.py:516` 的 `update_manifest(root, artifacts=artifact_inventory(root), …)` 遍历运行目录
下所有文件计算 SHA256（`io.py:155-163`，`read_bytes()` 对二进制安全），因此
`run-manifest.json.artifacts` 含 `.xlsx`/`.xmind` 及其哈希，确定性让这些哈希在两次运行间稳定。
其二，**`compare-runs` 比的是结构指纹，不是产物哈希**：`run_fingerprint()`（`reproduce.py:29-60`）
只取模块数、需求数、覆盖数、用例总数与门禁状态，`compare_fingerprints()`（`:63-85`）据此判漂移，
导出产物**不进入**该指纹。所以准确的说法是：确定性让导出产物**不破坏**复现基线（清单哈希稳定、
`--repro` 对拍不受影响），并提供「直接比 `artifacts` 里的 sha256」这条额外的产物级校验路径；
但它不是 `compare-runs` 的检测项。旧文档「导出产物可参与 `compare-runs` 对拍」的说法过于宽泛，
按代码应作上述限定。

> ⚠ 未验证：字节一致性的**跨环境**边界。同一 CPython + 同一 zlib 版本下已实测稳定；不同
> Python/zlib 版本间是否仍逐字节一致，本项目未做断言（`ZIP_DEFLATED` 的实现细节不在标准库
> API 承诺范围内）。若需跨机比对，应对**解压后的 part 内容**哈希（语义比对）而非 ZIP 字节。

## 8. 集成

### 8.1 CLI

```bash
prd2tc export --cases output/mall/v0.3/cases/cases                     # 两种格式 → cases/
prd2tc export --cases output/mall/v0.3/cases/cases --format excel
prd2tc export --cases output/mall/v0.3/cases/cases --format xmind --xmind-mode full
prd2tc export --cases output/mall/v0.3/cases/cases --out /tmp/cases.xlsx
prd2tc export --cases output/mall/v0.3/cases/cases --sheet-name "回归用例" --no-traceability
```

`cmd_export()`（`cli.py:186-213`）的两个约定：**路径推断**——`run_root = cases_path.parent.parent`
（`:191`），即 `--cases` 期望指向 `<output>/<product>/<version>/cases/cases`；未显式给
`--product/--version` 时分别取 `run_root.parent.name` 与 `run_root.name`（`:194-195`），因此默认
调用产出 `mall-v0.3-用例集.xlsx` 这类可辨识文件名而非一律「用例集.xlsx」（目录结构不同时显式传参）。
**失败即非零退出**——`except (FileNotFoundError, ValueError)` 打印 `✗ …` 并 `return 1`（`:208-209`）；
成功时逐文件打印 `✔ 已导出 <name>（<KB> KB，共 <N> 条用例）`（`:210-212`）。

### 8.2 `generate` 自动导出

阶段 3 用例落盘成功后自动导出两种格式，并**必须**用局部 `try/except` 包住
（`pipeline.py:503-515`）：`save_cases` → `save_traceability` → `write_stage(render_cases_md)`
之后调用 `export_cases(cases_dir=root/"cases"/"cases", formats=["excel","xmind"], root=root,
include_traceability=True, product=product, version=version)`，成功则
`exported = [p.name for p in export_info["files"]]`，`except Exception` 时 `exported = []` 并
`warnings.warn(f"导出 Excel/XMind 失败（不影响生成结果）: …")`，最后
`manifest_stage(..., exports=exported)`。**为什么必须局部 `try`**：外层有一个
`except BaseException`（`pipeline.py:523`）会把任何未捕获异常升级为整条运行 `status="failed"`；
导出是**附加产物**，它失败不该让已经生成好的用例被判失败。自动导出用全量（不传 `spec`）：
`generate` 的产物目录应自洽完整，筛选是**按需**行为。

### 8.3 服务端点

`GET /api/v1/exports/{run_id}/{fmt}`（`service.py:943-978`，参数含 `filename`、`xmind_mode`、
`sheet_name` 与四个筛选条件）：`fmt` 不在 `{excel,xmind}` → 400；`xmind_mode` 不在
`{case,full,module}` → 400（`:963-964`）；用例目录取 `output/"cases"/"cases"`（`_cases_for`，
`:910-918`），缺产物 → 404；筛选参数构造 `spec`（重复键 → `List[str]`，维度内 OR、跨维度 AND）；
`FileNotFoundError/ValueError` → 400 + `format_exception(exc)`（`:974-975`），**筛选为空因此以
400 报错**而非返回空文件；成功返回 `FileResponse(path, media_type=MEDIA_TYPES[fmt], filename=…)`
（`:977`）。

**下载名的响应头注入防护**：`filename` 完全由用户控制且直接进入 `Content-Disposition`，
未清洗时 `"` 或换行可造成**响应头注入**，`../` 可能被部分客户端当作路径。`_download_name()`
（`service.py:68-83`）只取基名（`Path(...).name`）、用
`re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)` 剔除引号与控制字符、`strip(" .")`、清洗后为空则
回退默认名、截断 120 字符，最后**强制** `ext = ".xlsx" if fmt == "excel" else ".xmind"`（`:75`），
避免把 xmind 内容存成 `.xlsx`。`MEDIA_TYPES`（`export/__init__.py:19-22`）：excel →
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`、xmind →
`application/vnd.xmind.workbook`（`service.py:130` 另用 `mimetypes.add_type` 注册 `.xmind`，
让产物列表接口也给出正确 Content-Type）。**路径安全**：
导出输出固定在运行产物内，端点**不接受**外部路径参数，因此没有穿越面（`:952`）；`--out` 只在 CLI 开放。

### 8.4 前端「导出」对话框

`ExportDialog.tsx` 让用户决定格式 / 文件名 / 保存位置 / 筛选：顶部 `Alert` 显示
`selectionLabel(selection)`，有筛选时提示「仅导出命中的用例；统计与需求覆盖会按该子集重算」，
无筛选时提示「导出全部用例」；格式为 `excel` / `xmind` 单选（`:92-95`）；文件名输入框实时回显
最终名 `resolved`，未填用默认名，未带扩展名自动补齐（`:51-54`）；保存位置优先
`showSaveFilePicker()`（Chrome/Edge），否则回退 `<a download>`（`api.ts:127-159`）；XMind 粒度
三选一并提示「节点过多时系统会自动降级为仅模块层」；Excel Sheet 名 `maxLength={31}` 与服务端
31 字符上限对齐。四个刻意的设计：**不再用 `window.open`**——那样文件名固定、位置只能是浏览器
下载目录，且**无法感知失败**（后端报错时浏览器会把错误页当文件存下来，`:28-33`）；
**错误详情从 Blob 读回**——`responseType:'blob'` 会把错误响应也包成 Blob，`detailFromBlob()`
（`:14-26`）读回文本再解析 `detail`，并回退 `FileReader`（jsdom/旧浏览器不一定有 `Blob.text()`）；
**空维度不发参数**（`api.ts:58-70`）——发送空数组会被服务端解析成「空集合」，语义上等于
「什么都不要」而非「该维度不限」；**默认文件名带日期与筛选片段**——`defaultExportName()`
（`api.ts:106-112`）产出 `<product>-<version>-用例集[-<scope>]-YYYYMMDD.<ext>`，与服务端
`name_suffix()` 同构（`selectionSuffix()`，`:82-91`），避免同一天多次导出互相覆盖，且每次打开
对话框都按当前格式与筛选重算默认名（`:45`）。筛选面板以 `ignore='*'` 一次取全三维度计数，
并用请求序号 `seqRef` 丢弃过期响应（`CaseFilterPanel.tsx:32-43`）。

### 8.5 产物登记与重跑清理

`.xlsx`/`.xmind` 经 `artifact_inventory()` 进入 `run-manifest.json.artifacts`（含 SHA256），
阶段 3 的 `exports` 字段另有文件名列表。artifact 接口按后缀把二进制分流到 `FileResponse`
（`service.py:128`、`:897-908`）——用 `read_text()` 读 `.xlsx` 会抛 `UnicodeDecodeError`。
重跑阶段 3 时清理清单**包含** `cases/用例集.xlsx` 与 `cases/用例集.xmind`（`:826-831`），
否则会留下与新版用例不一致的旧导出文件。

## 9. 失败策略与边界

| 情形 | 行为 | 依据 |
|---|---|---|
| 生成流程中的导出失败 | **不阻断主流程**，`warnings.warn` + `exports=[]`，运行仍成功 | `pipeline.py:504-515` |
| 用例目录为空/不存在 | `ValueError`（CLI → 退出码 1；服务 → 400） | `__init__.py:89-90` |
| 筛选后无命中 | `ValueError`，含总数与条件描述 | `__init__.py:92-98` |
| 空列表传给 `build_*_bytes` | `ValueError("没有用例可导出")` | `excel.py:294`、`xmind.py:200` |
| Excel 行数超 1,048,576 | `ValueError`（含实际条数与上限） | `excel.py:296-297` |
| XMind 节点数超 3000 | **自动降级为 `module`** + 告警；仍产出文件 | `xmind.py:203-215` |
| `module` 模式也超 3000 | `ValueError("请拆分用例集")`（理论不可达的最后防线） | `xmind.py:215` |
| 统计计算抛异常 | `summary=None`，**少一个 sheet**，导出照常 | `__init__.py:115-118` |
| `traceability.json` 缺失/损坏 | 只导出用例明细（+统计），无「需求覆盖」页 | `__init__.py:42-51`、`:112` |
| 筛选导出但 `requirements.json` 缺失 | 无法重算 → 不做「需求覆盖」页（不放全量矩阵的谎） | `__init__.py:108-110` |
| `requirements.json` 缺 `modules[].name` | XMind 回退纯模块代号 | `__init__.py:36-39`、`xmind.py:125` |
| 旧用例缺字段 | 一律 `.get()` + `EMPTY` 占位，不 KeyError | `excel.py:183-200`、`xmind.py:46-72` |
| 服务端 `filename` 含引号/换行/路径 | 清洗为安全基名，扩展名强制正确 | `service.py:68-83` |

贯穿原则：**能在产物内表达的降级就不要升级为失败**（少一个 sheet、导图粒度变粗都可接受）；
**但会让产物「看起来成功实则有错」的降级一律改为显式失败**（筛选为空、超行数、需求覆盖口径不一致）。

## 10. 实现取舍：为什么手写 OOXML / XMind

`README.md:9` 把「零第三方依赖」列为技术栈特征，`pyproject.toml` 运行时依赖为空。为导出引入
openpyxl（连带 lxml / et-xmlfile）意味着：安装体积与部署复杂度上升（离线环境、内网镜像都要
跟着处理）、依赖升级与安全告警进入维护范围、「标准库就能跑」这条在受限环境中真正有用的承诺
失效。而导出只需 OOXML 的**极小子集**（无公式/图表/共享字符串/数据透视）与 XMind 的
`content.json`，`zipfile` + `xml.sax.saxutils.escape` + `json` 即可覆盖（`excel.py:1-6`），
代码约 400 行。**代价是格式合法性必须自己保证**，由此产生三条约束：

1. **元素顺序、part 声明、关系 URI 全部自己守**：`[Content_Types].xml` 覆盖所有 part、
   `Target` 指向真实存在的 part、`Type` 是含 `/2006/` 的合法 URI、`cols` 在 `sheetData` 前、
   `autoFilter` 在其后、`cellStyles` 不能漏——任一条错了都可能被判「文件损坏」（结构断言见 §2.2）。
2. **必须用真实 Excel / XMind 验收**，而不是只用宽容解析器。openpyxl 交叉验证能发现「数据读错、
   列数不对、样式丢了」，但**发现不了**关系 URI 非法——宽容解析器不做该校验。权威判据只有一个：
   **真实 Excel 打开文件、无格式警告、内容完整**；XMind 同理。
3. **回归测试要锁行为而非常量**：`test_excel_p0_row_keeps_wrap_text` 取出第 2 行实际引用的样式
   索引再查 `cellXfs`；`test_excel_warn_fill_used_for_uncovered` 断言未覆盖行真的用到
   `STYLE_WARN`。只断言「常量已定义」会放过「定义了但从未引用」的缺陷。

已记录的验收数据：历史验收记录已并入本文，记录真实 Excel 打开后
`verify.xlsx OPEN-OK sheets=3`，三页为 `用例明细 11×13 / 需求覆盖 11×6 / 统计概览 7×3`，内容完整、
无修复弹窗；更早的 openpyxl 回读为 22 行 × 13 列，含冻结窗格与 `A1:M22` 筛选区。定位该缺陷的方法是
「以 openpyxl 生成的文件为对照，构造 hybrid 文件逐 part 替换，二分 `Workbooks.Open` 成败」，
一次定位到 `xl/_rels/workbook.xml.rels`——当 Excel 报损坏而所有解析器都说没事时，逐 part 二分是
唯一可靠的定位手段。

> ⚠ 未验证：本轮**没有**用真实 Excel / XMind 客户端打开产物（本环境无法启动它们）。上述
> `OPEN-OK` 数据来自前文档记录，未在本次复核中复现；改动 OOXML 结构（尤其 `_rels` 与
> `styles.xml`）后必须重新人工打开验收。本轮仅复核代码与测试：69 个导出/自然语言测试全部通过，
> 2000 条用例导出约 **90 ms / 104 KB（xlsx）**、**118 ms / 64 KB（xmind，case 模式）**。

## 11. 未验证项与已知限制

1. **筛选导出的 CLI 入口缺失**：`prd2tc export` 无 `--modules/--priorities/--tags`（§6.7）。
2. **XMind 客户端最终验收**：三个成员的结构由测试锁定，但「XMind 客户端能正常打开并编辑」
   本轮未复验。
3. **真实 Excel 打开验收**：本轮未执行（§10）。
4. **跨环境字节一致性边界**：同机同解释器已实测稳定；跨 Python/zlib 版本未验证（§7）。
5. **`--out` 与多格式的组合语义**：`out` 只在扩展名匹配时生效，否则回退 `cases/`
   （`__init__.py:137`、`:142`）；传目录给 `out` 等价于「忽略并使用默认路径」，行为确定但易误用。
6. **`STYLE_WRAP`（s=5）目前无实际使用方**：所有强调样式都自带 `wrapText`，它作为扩展位保留
   （`excel.py:24`）。
7. **统计视图只在 Excel 提供**：XMind 与 Markdown 没有等价的分面视图，筛选范围在 XMind 里
   只体现在页签名（`__init__.py:127`）。
