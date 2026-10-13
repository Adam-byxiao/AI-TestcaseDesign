# 多模态 PRD 摄取层（输入侧设计说明）

> 实现：`src/prd2tc/ingest.py`（核心）、`loader.py`（入口）、`prototype.py`（原型 → UI 契约）、
> `images.py`（图片发现/关联/视觉识别）、`llm.vision_completion`（多模态消息）、`service.py`（上传端点）。
> Python 3.10+，**运行时零第三方依赖**（`service` extra 是可选的 Web 适配层）。

## 1. 这一层要解决的问题

整条链路只认「一个 UTF-8 Markdown 文本文件」时，真实 PRD 的常见形态全都进不来：

| 层 | 原约束 |
|---|---|
| 上传 | `ALLOWED_PRD = {.md,.markdown,.txt}`、单文件、强制 `decode("utf-8-sig")` |
| 存储 / loader / 阶段 1 | `prds/prd-xxx.md`（一文件一份文本）；`read_text(encoding="utf-8")`；文本直接进 prompt |

于是 `.docx`（PRD 最常见交付格式，且内含图片）、Wiki/飞书导出的 `.zip`（md + 图片 + 附件）、
「图片本身就是需求」的多模态 PRD、`prd.md` + `图片和附件/` 的多文件组合、冻结评审版 `.pdf`
全部被拒或静默丢失。摄取层的职责是**把格式与生成解耦**：生成层只面对一个结构。
`Bundle` 的字段即摄取层的输出契约 —— `text`（需求正文，仅"需求载体"，按节内联）、
`sources`（全部来源的元信息，含 `role` 与 `note`）、`assets`（二进制资产）、
`warnings`（非致命问题，全部保留不静默）、`primary`（主文件名）、
`references` / `prototypes`（被判为接口文档 / 原型 HTML、**未进正文**的来源）。
三个派生能力决定它在系统中的位置（`ingest.py:141/154/975`）：`summary()` 给 CLI/前端做摘要；
`fingerprint()` 覆盖**正文 + 全部资产内容**并喂给 `configHash`（换图片或改附件必须让旧检查点失效）；
`to_json()` 落盘留痕（§12）。注意 `prdHash`（正文哈希）与 `bundleHash`（含资产）**必须分开**
（`pipeline.py:244-252`）：正文哈希是「已发布契约」，`diff` 用它判断新 PRD 是否与历史同一份；
掺入资产指纹会让它永不匹配，把未改动的 PRD 误判为 modified。

## 2. 支持的输入形态

一次运行可同时给多个输入项，`ingest.ingest()`（`ingest.py:694`）一起归一化：

| 输入形态 | 归一化路径 |
|---|---|
| 单个 / 多个文件路径 | `_norm_input`（`ingest.py:628`）；`loader` 把**第一个文件**设为主文件（`loader.py:73-77`） |
| 整个文件夹（含子目录）/ 压缩包（含嵌套） | `_iter_dir`（`ingest.py:668`）`rglob("*")` 递归；`.zip` 逐成员展开再分流（`ingest.py:758-777`） |
| 内存上传 / 文件夹上传 | `(name, bytes)` 走元组分支；`(name, bytes, origin, relpath)` 用第 4 项还原层级（`ingest.py:642-649`） |

格式支持矩阵（嗅探 + 解析手段）：

| 格式 | 处理方式 | 依赖 |
|---|---|---|
| `.md/.markdown/.mdown/.txt/.text/.log` | `decode_text` 直接作正文 | stdlib |
| `.docx` / `.docm` | ZIP+XML → **Markdown**（保留标题层级与表格），抽 `word/media/*` | stdlib |
| `.html` / `.htm` | **不进正文**，交 `prototype.py` 抽 UI 契约 | stdlib |
| `.pdf` | 内容流 Flate 解压 + `ToUnicode` CMap → 文本（best-effort） | stdlib |
| 图片与压缩包 | `.png/.jpg/.jpeg/.gif/.webp/.bmp` → **资产**（交 `images.py`）；`.zip` 递归展开（限深），逐成员按本表处理 | stdlib + 视觉模型 |
| 其它（含 `.xlsx`/`.pptx`） | 明确归类 `other` 并告警，不静默 | stdlib |

目录遍历与压缩包成员都忽略隐藏文件与 macOS 元数据（`.` 前缀、`__MACOSX`、`._*`，`ingest.py:764-772`）。
所有输出**确定性**（`sorted(rglob)`、`sorted(namelist())`、`sorted(word/media)`、原型侧按 key 排序）。

## 3. 格式嗅探：内容优先（magic number）

`sniff_kind(name, data)`（`ingest.py:171`）的判定顺序：

1. `data[:4] == b"PK\x05\x06"` → zip（空归档只有 EOCD，不单独处理会被判为 `other`）；
2. `data[:4] == b"PK\x03\x04"` → 打开 zip 看内部条目：`word/document.xml` → docx；
   `xl/workbook.xml` / `ppt/presentation.xml` → `other`（明确归类，不猜）；其它 → zip；
3. `data[:5] == b"%PDF-"` → pdf；
4. 图片魔数（PNG/JPEG/GIF87a/GIF89a/BM，或 `RIFF`+`WEBP`）→ image；
5. 扩展名兜底（`_EXT_KIND`）；未知/无扩展名时：含 `\x00` → other，
   能按 UTF-8 解码 → markdown（有 `^#{1,6}\s`）/ text。

**为什么内容优先**：上传文件的扩展名经常不可靠 —— 改名、无扩展名、Wiki 导出时被统一改成 `.txt`，
而魔数不会说谎。测试把这条锁死（`tests/test_ingest.py:88`：`sniff_kind("whatever.txt", docx_bytes) == docx`）。

### 3.1 实测教训：切片长度写错会让 docx/zip 嗅探**永久失效**

修复前写的是 `if data[:2] == b"PK\x03\x04":` —— 2 字节切片与 4 字节常量比较**永远为假**。
于是 zip 分支（含 docx 判定）从不触发，docx 一路落到扩展名兜底或 `other`：
拿不到 Markdown 转换、内嵌图片全部丢失。这不是「偶发漏判」而是**恒假**；
修复后取 4 字节切片（`ingest.py:177-180`），并在注释里保留这条教训。
同类隐患：`ingest._MAGIC_IMAGE`（`ingest.py:72`）与 `images._MAGIC`（`images.py:48`）
是**两份拷贝**，刻意如此以避免 `ingest ↔ images` 循环导入；改一处必须同步另一处。

## 4. 零依赖解析

### 4.1 DOCX → Markdown（`extract_docx`，`ingest.py:318`）

输出 **Markdown 而非纯文本**，因为下游 `split_document()` 依赖 `#` 分节：
丢层级会让长 PRD 的分段失效（也影响阶段 3 的模块上下文瘦身）。

标题级别按可靠性排序（`_docx_heading_level:298`）：① `w:pPr/w:outlineLvl` → `min(6, val+1)`；
② `w:pStyle` 查 `styles.xml` 的 `w:name`（`heading 1` / `标题 1`，`_docx_heading_styles:245`）；
③ `styleId` 形态正则兜底。

表格转 Markdown（`_table_md:283`）：按最大列数**补齐空单元格**，单元格内 `|` 转全角 `｜`
（否则撑破表格结构，`_cell_text:276`），单元格换行折成空格。段落处理
`w:t`（字）/`w:tab`（制表）/`w:br`·`w:cr`（换行）（`_para_text:262`）。

内嵌图片：`word/media/*` 按名称排序，逐个用魔数确认是图片；非图片的嵌入对象**计数并告警**
（"DOCX 中有 N 个嵌入对象非图片，已忽略"），不静默吞掉。最后把全角空格 `\u3000`
与不间断空格 `\u00a0` 归一为普通空格 —— Word 大量使用它们，不归一化会污染分节与后续匹配。
失败模式明确：不是 zip → `不是有效的 DOCX`；缺 `word/document.xml` → 明确报错；
XML 损坏 → `DOCX 主体 XML 损坏`（`ingest.py:328-336`）。

### 4.2 PDF → 文本（`extract_pdf`，`ingest.py:449`）

目标是覆盖**文本型 PDF**（Word/Markdown 导出），不追求完整 PDF 支持：

1. 扫描所有 `stream…endstream`，尝试 `zlib.decompress`（Flate）；解压失败且流里含
   `beginbfchar` 或 `BT` 时按未压缩处理；
2. 含 `beginbfchar`/`beginbfrange` 的流 → 构建 `ToUnicode` 映射（CJK 必需）：
   `bfchar` 是 `<src> <dst>` 对，`bfrange` 是 `<lo> <hi> <base>` 连续映射，
   单条范围最多展开 65536 项（`_pdf_tounicode:417`）；`<dst>` 按 UTF-16BE 解码并处理
   `FEFF` BOM（`_hex_to_str:439`）；
3. 内容流按文本算子切分：`Tj`/`TJ`/`'`/`"` 落字，`Td`/`TD`/`T*`/`ET` 换行；
   字面量 `(...)` 走 `_pdf_unescape`（八进制 `\ddd`、`\n` 等、续行），
   十六进制 `<...>` 优先按 **Identity-H 两字节一码**查表，否则单字节查表（`_pdf_stream_text:511`）。

**乱码检测**（`_looks_garbled:572`）：文本 ≥ 40 字符且「可打印字符占比 < 85%」即判低质并告警，
用 `isprintable()` 而非硬编码标点表（后者易漏引号转义、且要随语料维护）。

能力边界（必须知道）：

- **扫描件 / 纯图 PDF 抽不到文本** —— 本项目不含渲染器，返回空文本 + 明确告警
  （「请提供可复制文本的 PDF、源文档，或该页的图片以走视觉识别」），交调用方决定；
- 没有 `ToUnicode` 的 CJK PDF 会走 UTF-16BE 兜底，高概率乱码；
  但**只有**可打印占比跌破 85% 才会被 `_looks_garbled` 判出（§13）；
- 增量更新、加密 PDF、对象流（ObjStm）压缩的内容流不在覆盖范围内。

### 4.3 为什么不用第三方库

`python-docx` / `pypdf` 会带来依赖树、版本兼容与打包体积问题，而本项目的分发形态是
「单包 + 零运行时依赖」：装完即可跑，不引入供应链风险。代价是能力受限（见上），
因此设计上**把不确定性显式化**：抽不到就告警，抽得差也告警，绝不假装成功。

## 5. 编码回退链与换行归一化

`decode_text`（`ingest.py:214`）的链：
```
utf-8-sig → utf-8 → gb18030 → gbk → big5 → utf-16 →（全失败）utf-8 errors="replace"
```
上传的 PRD 可能来自 Word/记事本，是 GBK 而非 UTF-8；直接 `decode("utf-8")` 会抛
`UnicodeDecodeError` 把整个上传拒掉。回退链保证**永不抛异常**（`test_decode_text_never_raises`）。

后半段必须做**换行归一化**：`text.replace("\r\n", "\n").replace("\r", "\n")`。
不是洁癖：`Path.read_text()` 走 universal-newlines 会把 CRLF 折成 LF，而按字节解码会原样保留
`\r\n`。同一份文件因此出现两种正文形态，导致（实测）`sourceRefs` 偏移错位（同一位置 start 8 vs 6），
以及 `diff` 把**未改动**的 PRD 判为 modified —— 因为正文哈希变了。
测试同时断言「与 `read_text` 行为对齐」（`tests/test_ingest.py:127-142`），把这条不变量固化。

### 5.1 回退链的实测边界

回退链是「第一个成功的就赢」，因此**编码靠前者会劫持靠后者**。我实测了四种输入：

| 输入 | 期望 | 实测 `decode_text` |
|---|---|---|
| GBK「知识库配置需要支持上传文件」 | 正确 | ✅ 正确（该字节序列恰好是合法 UTF-8） |
| GB18030「中文€」 | 正确 | ✅ 正确 |
| **Big5「繁體中文測試」** | 正确 | ❌ 被 `gb18030` 先接住 → `篈磳いゅ代刚` |
| **UTF-16LE（无 BOM）** | 正确 | ❌ 被 `gb18030` 先接住 → `-N嗀` |

原因：`gb18030` 覆盖几乎全部字节空间，排在 `big5`/`utf-16` 之前时后两者在绝大多数字节序列上
**不可达**；UTF-16 只在有 BOM（`FEFF`/`FFFE`）时才对，因为 `utf-8`/`gb18030` 会先在 BOM 字节上失败。
因此繁体 Big5 与无 BOM UTF-16 会被静默解成乱码（不报错、不告警，因为文本侧没有乱码检测 ——
`_looks_garbled` 只用于 PDF）；要支持繁体语料需把 `big5` 前置或用统计式编码探测。

## 6. 主文件判定：`_pick_primary`

`_pick_primary`（`ingest.py:938`）取评分元组的**最小值**：
```python
score = (depth, named, rank, -len(text), order)
#         ①      ②      ③      ④          ⑤
```

五级信号（逐级比较，任一级分出胜负即停止）：
⓪ `preferred`（调用方显式指定，`loader` 传用户给的第一个**文件**，`loader.py:73-77`）—— 最强信号，直接返回；
① `depth` 相对层级，越浅越优先；
② `named` 文件名 stem 含 `prd`/`需求`/`requirement`/`spec`；
③ `rank` 格式优先级 `.md`(0) → `.docx`(1) → `.pdf`(2) → 其它文本(3)；
④ `-len(text)` 正文最长者；⑤ `order` 输入顺序（稳定兜底）。

**为什么不能只看长度**：实测把「PRD + 图片和附件」一起摄入时，附件 `rag-service.md`
（43KB 接口文档）比真正的 PRD（17KB）**长得多**；按长度会把附件当正文、PRD 反被降级为附件，
生成结果的规模与内容整体跑偏。

**为什么 `depth` 必须排在名称线索之前**：目录结构是用户意图的直接表达，且与语言无关 ——
「主文档放根目录、资料放子目录」是通用约定，`depth` 只依赖路径形态；而名称线索**依赖语言**
（要同时覆盖 `prd`/`需求`/`requirement`/`spec`，换个语种或命名习惯就失效）。
`test_depth_outranks_name_hint`（`tests/test_ingest.py:403`）专门做隔离验证：让**深层**文件独占
名称线索、浅层文件无线索，浅层必须胜出 —— 否则把 `depth` 从评分里删掉测试也会通过（变异测试曾漏报）。

### 6.1 `depth` 是怎么算出来的

`depth` 来自**共同父目录**的相对层级，而不是各自目录：磁盘路径由 `loader._common_base()`
（`loader.py:88`）对全部输入路径求 `os.path.commonpath` 后传给 `ingest(base_dir=...)`；
`_norm_input`（`ingest.py:660-664`）算 `len(p.resolve().relative_to(base).parts) - 1`，
不在 `base` 之下时视为 0（显式单文件）；`_iter_dir`（`ingest.py:686-690`）同样按 `base` 算，
失败时回退到目录内相对层级；文件夹上传则由浏览器给的 `relpath` 推
（`len(Path(relpath).parts) - 1`，`ingest.py:647`）。

实测形态：`prds/<id>/我的PRD/PRD.md` 与 `prds/<id>/我的PRD/图片和附件/rag-service.md`
必须是 depth 0 与 1。若不传 `base_dir`，两者都按「各自目录的 0 层」算，
`rag-service.md` 就与 PRD 同级，层级线索完全失效（`test_depth_from_base_dir_for_disk_paths`）。

> 注意：按 `relpath` 直接推 depth 时，根目录的 PRD 是 **1** 而非 0（`我的PRD/PRD.md` 有 2 段）。
> 这是**单调等价**的 —— 比较的是相对大小而非绝对值。两套测试断言的是各自路径的数值，
> 因此分别看到 1（`tests/test_ingest.py:436`）与 0（`test_depth_from_base_dir_for_disk_paths`）。

**排序副作用**：`_assemble` 里**必须先按输入顺序稳定排序，再选主文件**（`ingest.py:906-910`）。
反过来的话 `primary_idx` 会指向排序前的下标，正文与文件名错位（开发中踩到过）。

## 7. 附件按角色分流（生成质量的上限所在）

`_assemble`（`ingest.py:784`）把每个来源归入一种角色，写入 `Source.role`：

| role | 含义 | 是否进正文 |
|---|---|---|
| `primary` | 主文件 | ✅ 正文起始段 |
| `attachment` | 其它需求载体 | ✅ 以 `## 附件：<name>` 分节内联 |
| `reference` | 接口/服务文档 | ❌ 默认排除（`Bundle.references` 留痕） |
| `prototype` | HTML 原型 | ❌ 只解析为 UI 契约（`Bundle.prototypes` 留痕） |
| `asset` | 图片 | ❌ 交视觉识别 |
| `skipped` | 超限 / 重复 / 空正文 / 格式不支持 | ❌ 带 `note` 说明原因 |

### 7.1 `_looks_like_reference` 的判据（保守）
```python
_REFERENCE_NAME_RE = (service|api|interface|sdk|spec|contract|protocol
                      |接口|服务文档|接口文档|协议|字段说明)   # 作用于文件 stem
_ENDPOINT_RE       = \b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b[^\n]{0,20}?(/[A-Za-z0-9_\-/{}.]+)
```
判定（`ingest.py:591`）：① 文件名 stem 命中上述词（**主信号**）；
② 或前 200,000 字符中出现 **≥ 8 个去重后的 `METHOD /path`**。
刻意保守：**宁可漏判，也不误伤真实 PRD** —— 真实 PRD 不会自称 `service`/`接口文档`，
也不会列 8 个以上 HTTP 端点。命中的来源被排除并**逐条告警**：
「X 判定为参考资料（接口/服务文档），未并入需求正文；如需纳入请加 `--include-references`」。
主文件**永不**被判为 reference：所有分支都带 `name != primary_name` 前置
（`ingest.py:821/842/857`）。

### 7.2 实测事故：3993 字符的 PRD 被喂成 73159 字符

一次真实运行（提交 `fdb62df`）产出 **319 条需求 / 43 个模块**。根因不在模型，在输入：

| 输入 | 字符 | 占比 | 性质 |
|---|---:|---:|---|
| 真实 PRD 正文 | 3,993 | 5.5% | 真正的需求（约 30 条） |
| 原型 HTML 源码 | 42,111 | 57.6% | JavaScript 源码 |
| `rag-service.md` | 26,981 | 36.9% | 通用接口文档 |
| **合计** | **73,159** | **94.5% 与需求无关** | 却被要求「穷尽抽取需求」 |

污染的直接证据：结果里出现 `REQ-RAG-022 system 含 # 蔚来RAG优化 时启用蔚来单路检索` ——
而该项目与蔚来毫无关系。来源是 `rag-service.md`（《Yiwise RAG 服务接口文档》）
里一条**客户专用策略开关**。而 PRD 对这份附件的定位写得很清楚：

> **5.5.5 RAG 接口参考** —— 原始资料：下方保留接口清单标注图及完整 `rag-service.md` 附件，
> 便于研发查看请求、响应与回调细节。

即**查阅用细节，不是需求**。原型侧同理：42K 源码占输入 57.6%，模型据其 JS/CSS 抽出
「字体栈 Menlo/Consolas/PingFang SC」「`#toast` z-index=99」这类实现细节当需求。

**修复后同一份输入：正文 73,159 → 3,993 字符（↓95%）**，主文件正确、8 张图仍全部入资产、
蔚来污染消失、`ui-spec.json` 仍生成。

### 7.3 逃生阀 `include_references` 与「只找到参考资料必须报错」

`include_references=True` 时 `_is_ref` 退化为恒假（`ingest.py:800`），
参考资料重新并入正文（role 变 `attachment`）。

> ⚠ **未验证 / 现状差异**：代码注释与告警文案都写「加 `--include-references`」，
> 但**当前 CLI 没有这个参数** —— `cli.build_parser` 的 `generate` 子命令未注册它，
> 实测 `prd2tc generate --include-references` 报 `unrecognized arguments`；
> `loader.load_bundle` / `GenerateRequest` 也没有透传。
> 因此这个逃生阀**目前只在库 API 层可用**（直接调用 `ingest.ingest`），
> 从 CLI 或 Web 上传路径无法打开。属文案与实现不一致的接口缺口。

若全部文本来源都被判为 reference，**不能**把它当需求正文静默产出 —— 那正是「蔚来」污染的形态。
`_assemble` 于是显式抛错（`ingest.py:892-899`）：
```
未找到需求正文：<名单> 均被判定为参考资料（接口/服务文档），不作为需求来源。
请提供 PRD 正文（需求文档），或用 --include-references 强制纳入。
```
另外两条「无正文」分支有意区分：**只有图片** → 合法，`primary` 取第一张图名、正文留空，
告警「按『纯图片 PRD』处理，将完全依赖视觉识别结果」；**既无文本也无图片** →
抛「输入中没有可解析的文本或图片内容」。输入全军覆没时（`pending` 为空，`ingest.py:730-738`）
把已收集的告警带进异常，并**逐条截断到 160 字符**、只留前 5 条 + 总数 ——
曾把整个路径列表的 `repr` 塞进消息，报错长到无法阅读。

## 8. 原型 HTML → UI 契约

### 8.1 为什么需要：设计真值不可达，模型必然编造

实测运行 `c653dc92`：PRD 全文 `data-testid` 出现 **0** 次，原型也没随 PRD 送入模型，
而提示词硬性要求 `target` 用「元素中文名 (data-testid)」格式。结果：

| 项 | 实测 |
|---|---|
| 用例引用的去重标识符 | **143 个，命中契约 0 个（100% 编造）** |
| URL 路由 | **7 种，全部不存在**（原型是单页应用，无路由） |
| 弹窗交互 | PRD §6.1.3 只写「弹窗提醒」，用例臆造出「保存/放弃/取消」三选项弹窗（4 条用例、约 30 个步骤）；真值是通知栏文案 + `[查看未保存修改]`，**不存在阻塞式三选项弹窗** |

这与图片能力要解决的是同一类问题：**真值不可达 → 只能编造**。
消除「要求模型填写输入里根本没有的字段」这类设计缺陷，比事后加门禁更有效。

### 8.2 解析策略：静态 HTML 只是外壳

原型是**单页 JS 应用**，真正的 DOM 在 `<script>` 的模版字符串里，静态 HTML 只剩
`nav`/`app`/`modal`/`toast` 外壳。因此 `extract_ui_spec`（`prototype.py:432`）同时解析：

| 来源 | 抽取内容 |
|---|---|
| 静态 HTML 属性 + JS 文本 | `id` / `class` / `data-*` 锚点 |
| JS 模版字符串 | `id=` / `class=` / `<button>` 文案 / `<th>` 列头 |
| JS 状态声明 | `let page='config',tab='main',dirty=false,...` |
| JS 数组字面量 | `navs`（导航页）/ 页签数组 |
| JS 函数调用 | `modal(...)`（标题与全部按钮）/ `toast(...)`（提示文案） |

产出 `UiSpec`：`pages` / `tabs` / `anchors`(id·class·data-attr) / `fields`
（id·label·placeholder·maxlength）/ `buttons` / `modals` / `messages` / `tables`（列头）/
`state` / `interactions`（onclick 函数名）/ `has_url_routing`。

几处被测试锁死的实现要点：

- **嵌套数组必须平衡扫描**：`navs` 是 `[[k,i,label],...]`，正则 `\[[^\]]*\]` 会在第一个 `]` 截断
  （实测导航页只解析出 1/3）。`_balanced`（`prototype.py:155`）跳过引号与转义做括号配对；
- **`<th>` 正则不能写成 `<th[^>]*>`**：`re.I` 下会连 `<thead>` 一起匹配，非贪婪 `(.*?)</th>`
  会把整段表格吞成一项（实测踩到），必须用 `<th(?:\s[^>]*)?>`（`prototype.py:115`）；
- **列头不能逐字符拆**：曾产出 `["会","话","","I","D"]` 这类结果（`_extract_tables:417`）；
- **弹窗按钮取自第 3 个实参**：原型里 `ok === '关闭'` 只有 1 个按钮，否则为 `["取消", ok]`，
  **从不超过 2 个**；因此不能扫描函数体（会抓到无关按钮），也不能把函数定义
  `modal(title,body,ok,…)` 或动态标题 `modal(d.name,…)` 当真实弹窗（`_extract_modals:391`）；
- **`class` 属性即「真实使用过的类」**：不再用样式表反向过滤 —— 样式表里出现恰是该类真实存在的旁证。

`merge_specs`（`prototype.py:494`）合并同产品多个原型页面：pages/tabs/anchors/modals 按 key
去重排序，fields 按来源顺序拼接，buttons/messages/interactions 取并集，`has_url_routing` 取或。
`parse_prototype`（`prototype.py:484`）是文件入口：UTF-8 读（`errors="replace"`），
校验含 `<(html|body|div|script)` 才算 HTML，否则抛「不是可识别的 HTML 原型」。

### 8.3 契约如何进入 prompt

`render_ui_spec`（`prototype.py:567`）生成紧凑清单，注入**阶段 3 的 system 前缀**
（`stages.stage_3_system:216`，`pipeline.py:475`）—— 放 system 而非 user，因为契约对整轮运行恒定：
可命中 prompt 缓存，且能强约束所有批次。渲染内容与强约束：

- 标题即声明「以下标识符与文案为真实值，必须原样使用；不得编造新的标识符或路由」；
- **路由存在性**：有路由 → 「可直接使用其地址」；无路由 → 「⚠ 单页应用，不存在 URL 路由：
  `goto` 请使用页面键（如 `page=config`），**不得编造 `/xxx` 路径**」；
- 导航页/页签（`key（label）`）、id 锚点、class 锚点（`max_anchors=80` 截断并报「另有 N 个」）、
  表单字段（id + 标签 + 占位 + 上限）、按钮文案、表格列、弹窗（「括号内是它的**全部**按钮，
  不得增加选项」）、提示/校验文案（前 40 条）；
- 收尾明确：`target` 写法为 `页面元素中文名 (id=真实id)` 或 `(class=真实class)`，
  **不要输出 data-testid**（原型中没有该属性）。空的 `UiSpec` 渲染为空串，不注入噪声。

原型识别有两条入口（`pipeline.py:258-262`）：① `--attach` 显式指定（CLI 入口）；
② **从已摄取的文件里自动识别**（`_auto_prototypes:147`），覆盖「UI 上传文件夹」的常见形态 ——
筛选 `kind == "html"` 且 `role != "skipped"`，并要求 `origin` 是真实存在的磁盘路径
（因此上传/压缩包里的 HTML 也能落盘后解析）。解析失败只告警，绝不中断：原型是增强输入，不是硬依赖。

### 8.4 `validate_targets`：按种类校验

`validate_targets(cases, spec)`（`prototype.py:720`）在门禁里校验用例的 `target` 与 `goto`；
契约为空（既无 anchors 也无 pages）时直接放行，不做无依据的判定。按种类建池
（`_annotation_pools:645`）：

| 种类 | 池 |
|---|---|
| `id` | 仅 `anchors[kind=id]` |
| `class` | 仅 `anchors[kind=class]` |
| `page` | 页面 `key` ∪ 页面 `label` |
| `tab` | 页签 `key` ∪ 页签 `label` |
| `text` | 按钮 ∪ 弹窗标题/按钮 ∪ toast 文案 ∪ 字段 label/placeholder（宽松，见下） |
| `any` | id ∪ class（旧式 `data-testid=` 与裸标识符的归属） |

**为什么必须分种类**：`(id=tools)` 里的 `tools` 在契约里确实存在，但它只作为**页签键**存在，
不是任何元素的 id。早先只校验「值是否存在」（不分种类），这类跨命名空间误用被全部放行 ——
实测 **17 处**（`id=tools`×9、`id=skills`×5、`id=main`×3）。现在 `id=`/`class=`/`page=`/`tab=`/`any`
都严格，报错会指明「契约有 N 个同种类取值」并给出样例。

**`text=` 刻意宽松**（`_STRICT_KINDS`，`prototype.py:641`）：可见文案可能是**用例自己造的数据名**
（如 `text=测试草稿名称`），严格校验会产生假阳性；而假阳性会重演「门禁与提示词矛盾 → 重试无法收敛」
的老问题。`name=`/`label=`/`placeholder=` 视为 `text=` 的别名。未知种类且取值也不存在时，
报「未知种类」并提示改用合法种类。

**无括号变体也必须识别**（`_BARE_ANNOT_RE`，`prototype.py:627`）：`知识库配置页 page=knowledge`
这种写法没有括号，若只匹配 `(...)$`，模型换个写法即可绕过校验（实测存在这种变体）。
因此先试括号标注，再试裸尾随标注，两条路径都走同一套 `_check_annotation`。

路由类检查（原型**无** URL 路由时）：`target` 出现 `/xxx` 且不在 `page_keys` 中 → 报
「出现 URL 路径，但原型是单页应用」；`action == "goto"` 且 `data` 以 `/` 开头 → 报
「goto 的 data 为 URL，但原型无 URL 路由」；`goto` 的 `data` 非空非路径时，
必须落在页面键、页面名（子串匹配）或已知锚点上。

## 9. JS 转义解码（`js_unescape`）

压缩/工具生成的原型把中文写成 `'\u672a\u4fdd\u5b58\u4fee\u6539'`。若直接取字面量，
契约里的标题与按钮全是一串反斜杠文本，甚至被 `_is_ui_label` 的「至少含一个中文」判定**误杀** ——
按钮会被整批丢掉。`js_unescape`（`prototype.py:130`）只解 `\uXXXX` 与 `\xXX`，
解完再合并代理对（`\ud83d\ude00` → 😀），避免留下孤立代理导致后续编码失败；
非法转义（`\uZZZZ`）原样保留。

**为什么只对「已提取的字符串值」解码，而不对整段 JS 解码**：后者会把 `\u0027`（单引号）
变成 `'`，破坏引号配对，导致后续所有正则整体错位。这是一个「解码越彻底、解析越崩」的典型陷阱。
调用点全部在取值之后：`_literal`（字面量/三元候选）、`_clean_label`（模版串残留）、`placeholder`、
`_parse_pairs` 的页签 label、`toast` 文案。`test_escaped_and_plain_prototypes_agree`
（`tests/test_prototype.py:434`）断言同一原型的转义/明文两种写法产出**等价契约**。

`_clean_label`（`prototype.py:240`）另处理模版残留：抹掉 `'+` / `+'`，把三元
`(cond?'收起':'展开全部')` 展开为 `收起 / 展开全部`，把 `${...}` 折叠为 `…`，最后去掉孤立引号。
一个被测试抓出的坑：去掉三元条件前缀时**必须用 ASCII 限定标识符**
`[A-Za-z_$][A-Za-z0-9_$]*`；写成 `[\w$]+` 时 Unicode 下 `\w` 会匹配中文，
把 `文档名称` 的前缀吃成 `称`（实测踩到）。

## 10. 图片能力

### 10.1 发现（`discover_refs`，`images.py:135`）

解析 Markdown `![](...)` 与 HTML `<img>`，按出现位置排序，为每条引用记录：
`index`（1 起）/`alt`（图注）/`target`（URL 或相对路径）；`section` = **最近的上文标题**
（按标题位置线性推进，避免 O(n²)）；`context` = 引用前后各 300 字符、压平空白、去掉图片语法本身、
截 400 字符；`remote` = 是否 `^https?://`。

### 10.2 关联：最长公共子串 + 全局唯一分配

关联逻辑只有**一份实现** `assign_by_name`（`images.py:262`），被两条来源共用：`resolve_refs`
（候选来自磁盘文件）与 `prepare_from_assets`（候选来自内存资产）。两处各写一套会出现
「改了一处、测试仍全绿」的假安全感（`find_local` 曾与 `resolve_refs` 重复实现，
变异测试因此没抓到「退回包含判定」的回归）。四阶段分配，**每个候选至多被一个引用占用**：

| 阶段 | `match` | 规则 |
|---|---|---|
| 0 | `explicit` | `target` 本身就是存在的相对路径（在候选池判空**之前**处理，否则「图就在 PRD 旁边但目录名不常规」会被提前 return 漏掉） |
| 1 | `filename` | `target` 的 basename 与候选同名/同形（`_norm` 归一后比较） |
| 2 | `alt` | 图注与文件名 stem 的**最长公共子串**评分，贪心全局唯一，阈值 `MIN_ALT_OVERLAP = 3` |
| 3 | `order` | 顺序兜底，**仅在「剩余引用数 == 剩余候选数」时启用** |

**为什么用最长公共子串而不是「包含」**：PRD 图注通常很长、文件名很短，二者**互不包含**：

| PRD 图注 (alt) | 实际文件名 | 包含 | 最长公共子串 |
|---|---|---|---|
| 业务场景：新增、删除与带行号的内容编辑 | `业务场景编辑.png` | ❌ | **4**（业务场景） |
| 内嵌文字测试：配置与测试并排、长工具结果折叠 | `文字测试界面.png` | ❌ | **4**（文字测试） |
| 知识库配置：文档列表及添加入口 | `知识库配置与添加.png` | ❌ | **5**（知识库配置） |
| 知识命中测试：片段、文档来源及评分 | `知识库命中测试.png` | ❌ | **4**（知识命中测试） |

实测：**「包含」判定只能关联 3/8（38%），最长公共子串 8/8（100%）**。
`longest_common_substring`（`images.py:201`）用滚动数组，空间 O(min(n,m))。
阈值取 3：CJK 下 3 字已具区分度（「知识库」可区分「知识库配置」与「知识库命中测试」，
而 2 字的「配置」太泛会被拒）。

**`order` 为什么只在数量相等时启用**：实测 PRD 的图序与目录内文件名排序**并不一致**
（`智能体配置整体.jpeg` 排在目录第 5 位，却对应第 1 处引用）。数量不等时按顺序硬配会成片错配，
因此只在数量恰好匹配时兜底，并标记 `match="order"` 供人工复核。

`candidate_dirs`（`images.py:168`）给出约定目录名（`<stem>.assets`、`<stem>_files`、`assets`、
`images`、`img`、`attachments`、`media` …）；`load_bundle` 另有一条兼容路径：
**单文件输入时自动带上同级约定资源目录**（`loader.py:78-82`），对应「PRD 下载为 md、
图片另存同目录」的常见形态。`extra_dirs`（`--images-dir`）优先级最高。

> 注意 `candidate_dirs` 的首项是文件所在目录**本身**，`load_bundle` 必须把它排除
> （`d != p.parent`），否则会把整个目录吞进来（`loader.py:81-82`）。

### 10.3 读取与上限

`read_image`（`images.py:366`）**不信任扩展名**：按魔数判定类型，扩展名只用于错误提示。
改名为 `.png` 的文本文件必须被拒绝 —— 否则损坏内容会被送进视觉模型，浪费调用且污染证据。
失败条件是明确的三类：空文件、超过 `MAX_IMAGE_BYTES = 8MB`、魔数不识别。
8MB 的由来：图片以 base64 内联，体积约放大 1.33 倍，8MB 已接近多数 Provider 的请求上限。
远程图片需显式开启（`fetch_remote:385`，`--fetch-images`）：多读 1 字节以判定超限，
并校验 `Content-Type` 前缀（`sniff_mime` 的 fallback 只在 `image/*` 时生效）。

### 10.4 视觉识别、缓存与降级

`analyze_images`（`images.py:475`）逐图调用 `vision_completion`，system 为
`VISION_INSTRUCTION`（`images.py:403`）—— 明确要求「只依据图中可见内容、逐字抄录、**禁止猜测**、
看不清写不确定」，特别要求标注被红框/箭头圈出的**本次改动点**（`annotated`），
以及角标/计数器/状态标签这类「可断言的强证据」。缓存键 `cache_key`（`images.py:429`）：
```
sha256( 图片内容 sha256 \x00 模型名 \x00 VISION_PROMPT_VERSION ) → 前 32 位
```
落盘 `analysis/.image-cache/<key>.json`。**必须包含 `VISION_PROMPT_VERSION`**：
改了抽取要求却命中旧结论，会产出与提示词不匹配的证据。缓存体**刻意排除**
`index`/`source`/`alt`/`section`/`sha256`/`cached`，命中时这些字段从**当前引用**取 ——
缓存按图片内容寻址，同一张图在不同文档里的序号可能不同（早期版本把缓存体直接当构造参数，
`ImageInsight.index` 是必填位置参数，命中即崩）。

**降级原则**：任何单点失败都只告警不中断。单图分析抛异常 → 经 `reporter` 报
「⚠ 图片 N 分析失败，已跳过」并继续下一张；模型整体不返回结论 → 追加上限告警；
`analyze=False`（stub）或未提供视觉配置 → 只做发现与关联，产出可复核的图片清单并说明
「未做视觉识别」。渲染时显式标注「**来自图片的视觉识别，属推断结论**；与 PRD 明文冲突时
以明文为准，并在用例中标注 `[推断]`」。

`render_image_spec`（`images.py:670`）把结论渲染为证据块，注入位置与 UI 契约不同：
**阶段 1 的 user 输入区**（`stages.build_stage_1_user:157`，`pipeline.py:399`）——
它是「补充的 PRD 事实」，属于被测对象的一部分，需与 PRD 全文并列供模型对照；
阶段 3 则注入 system 前缀（`pipeline.py:475`）。

### 10.5 与摄取层的对接：`prepare_from_assets`

`prepare_from_assets`（`images.py:552`）面向**已在内存的资产**（上传 / 压缩包 / DOCX 内嵌 / 目录），
因此不再需要「先把图落到某个目录、再用 `--images-dir` 指过去」的绕道。
`pipeline._load_images`（`pipeline.py:180`）是唯一调用点，传 `bundle.images()` 与 `bundle.text`。
有 `![]()` 引用时用引用补齐 alt/所在小节；**未被任何引用认领的资产也会全部纳入**
（纯图片 PRD、或图片未写进 Markdown），`match="asset"`、`alt` 回退为文件名，
序号从现有最大 index 继续递增。

> ⚠ **未验证**：`prepare_from_assets` 接收 `allow_remote`，但它使用的 `bundle.assets`
> 已全部是本地字节、`ImageRef` 均由资产构造（`remote=False`），
> 因此该路径下 `--fetch-images` 对「正文里的远程图片引用」可能**没有作用**
> （远程引用只出现在 `images.prepare()` 的磁盘分支，而该函数当前无调用点）。仅据代码路径判断，未端到端实测。

## 11. 上限与防御

| 常量 | 值 | 位置 | 作用 |
|---|---|---|---|
| `MAX_SOURCE_BYTES` | 64 MB | `ingest.py:54` | 单个输入上限；超限记 `skipped` 并告警，**不影响其它输入** |
| `MAX_ZIP_DEPTH` / `MAX_MEMBERS` | 3 / 500 | `ingest.py:55-56` | 嵌套压缩包深度、单压缩包成员数；超限跳过或取前 500 并告警 |
| `MAX_TEXT_CHARS` | 2,000,000 | `ingest.py:57` | 归一化后正文上限；超限截断并提示「请拆分 PRD 后分段生成」 |
| `MAX_IMAGE_BYTES` / `_image_files` limit | 8 MB / 2000 | `images.py:45` / `:187` | 单图上限（base64 约 1.33× 后接近 Provider 请求上限）；单目录扫描图片数上限 |
| `VISION_PROMPT_VERSION` | `"1.0"` | `images.py:42` | 参与图片缓存键 |
| `MAX_UPLOAD_FILES` / `MAX_UPLOAD_BYTES` / `MAX_UPLOAD_TOTAL_BYTES` | 400 / 64 MB / 256 MB | `service.py:22-24` | 上传文件数、单文件、单次总量上限；超限提示「请分批」 |
| `max_anchors` / `max_texts` | 80 / 60 | `prototype.py:567` / `images.py:670` | UI 契约锚点、单图「图中原文」的渲染上限 |

截断语义要理解清楚：`MAX_TEXT_CHARS` 是**拼接之后**对 `body` 整体切片（`ingest.py:923-926`），
因此截断可能落在某个附件中间 —— 告警说明发生了截断，但不指出边界；图片资产不受该上限影响。

### 11.1 上传端点与目录穿越防护

`service.upload_prd`（`service.py:443`）接受 `file`（单文件，向后兼容）与 `files`（多文件，推荐）
两个参数并取并集；类型**必须**声明为 `List[UploadFile]`，用 `Any` 时 FastAPI 不按列表解析会返 422。
允许的扩展名 `ALLOWED_PRD`（`service.py:19`）：
`.md .markdown .mdown .txt .text .docx .docm .pdf .zip .png .jpg .jpeg .gif .webp .bmp`。

每个文件**保真落盘**（原始字节，不做文本化）—— 图片与 docx 的二进制一旦被当文本处理就毁了；
格式判定与解析交给 `ingest`。落盘后立刻跑一次 `ingest.ingest(payloads)`（`service.py:507`），
把「哪些是正文、哪些是资产、有哪些告警」反馈给前端，避免用户上传后才发现格式不被支持。

三重防护：① `_safe_filename`（`service.py:27`）只取基名、剔除 `<>:"|?*` 与控制字符、
去掉前导 `.`（避免落成隐藏文件）、截断 120 字符，空则回退 `upload.bin`；
② `_safe_relpath_parts`（`service.py:108`）把浏览器给的相对路径逐段清洗，
**丢弃 `.` / `..` / 空段 / 含 `:` 的段（盘符）**，返回除文件名外的目录段；
③ 同名冲突时追加 `-1`/`-2` 序号（`service.py:490-493`），避免多选或深层目录撞名互相覆盖。

**为什么必须还原目录结构而不是拍平**：拍平后「根目录的 PRD」与「子目录里的附件」在磁盘上
不再可区分，摄取层赖以判定主文件的 `depth` 会全部退化为 0（实测：`图片和附件/rag-service.md`
因此与 PRD 同级，层级线索失效）。落盘后传给摄取层的是
`(name, bytes, "upload", target.relative_to(folder))`（`service.py:496`），第 4 项就是 `depth` 的来源。

`loader._common_base` 对**磁盘路径**求共同父目录（`loader.py:88`）：只取路径输入，
目录取自身、文件取父目录；`os.path.commonpath` 抛 `ValueError`（如跨盘符）时返回 `None`，
此时 depth 全部退化为按各自目录计算 —— 不是致命错误，但会削弱主文件判定。

## 12. 产物留痕：可回答「为什么选了这份当正文」

三份 JSON 落在 `<out>/<product>/<version>/analysis/`：

- **`prd-bundle.json`**（`ingest.to_json`，`pipeline.py:271`）：`primary`、`textChars`、
  `sources[]`（`name/kind/size/sha256/origin/role/textChars/note/depth`）、
  `assets[]`（元信息，**不含字节**）、`warnings`、`fingerprint`、`summary`
  （含 `referenceCount`/`prototypeCount`）。这是排查输入问题的第一站：
  `role` + `note` 回答**为什么这份被排除**，`depth` 回答主文件判定依据，
  `textChars` 回答每份来源贡献多少字符，`fingerprint` 与 `run-state.json` 的 `bundleHash` 对应，
  可判断「换了哪份输入导致检查点失效」。
- **`ui-spec.json`**（`prototype.to_json`，`pipeline.py:141`）：`source`（全部原型文件名）、
  `pages`/`tabs`/`anchors`/`fields`/`buttons`/`modals`/`messages`/`tables`/`state`/`interactions`/
  `hasUrlRouting`。回答「这套标识符是从哪份原型来的」。`run-manifest.json` 的
  `attachments[].autoDetected` 与 `sha256` 记录每份原型来源，`uiSpecHash` 进 `configHash`
  （改了原型就必须重新生成）。
- **`image-spec.json`**（`images.to_json`，`pipeline.py:208`）：`promptVersion`、`refs`
  （`index/alt/target/section/context/resolved/match/remote`）、`insights`
  （`kind/summary/annotated/uiElements/fields/states/texts/layout/flows/notes`）、`warnings`、`summary`。
  回答「哪张图关联到哪条引用、用什么方式关联（`explicit`/`filename`/`alt`/`order`/`asset`）、
  哪些没识别成功」—— `match="order"` 的低置信关联应人工复核。`imageSpecHash` 同样进 `configHash`；
  stub 模式下不调用视觉模型，但仍产出该文件（纯清单 + 告警）。

## 13. 已知边界与未验证项

| 边界 | 说明 |
|---|---|
| **Big5 / 无 BOM UTF-16** | 被 `gb18030` 抢先解成乱码，且不报错（§5.1，已实测） |
| **扫描件 PDF / 无 ToUnicode 的 CJK PDF** | 前者抽不到文本只给告警（需用户补图或源文档）；后者靠 UTF-16BE 兜底，乱码若恰好全为可打印字符则不会被 `_looks_garbled` 拦下（未构造样本验证） |
| **`--include-references`** | 文案存在、CLI 参数不存在（§7.3，已实测） |
| **`--fetch-images` 与内存资产路径** | 对正文里的远程引用可能无效（§10.5，仅据代码路径判断） |
| **`.xlsx` / `.pptx`** | 明确归类为 `other` 并告警，不解析 |
| **`order` 关联** | 置信度低，目录排序与 PRD 图序不保证一致，需人工复核 |
| **视觉模型能力 / 图片结论** | 需 Provider 支持图片输入，不支持时告警并跳过；结论属推断，可能误读，不替代人工评审 |
| **截断边界** | 正文超 `MAX_TEXT_CHARS` 时可能截在附件中间，告警不指出边界 |
| **未做** | OCR 精读小字、图表数值抽取、图片与需求的多对多关联、PDF 增量/加密/对象流 |

## 附录：调用链速查
```
CLI:  prd2tc generate --prd X --prd DIR [--images-dir D] [--attach P.html]
  cli.cmd_generate:46   loader.load_bundle(prds, extra_dirs=...)
    loader.load_bundle:46   取第一个文件为 preferred；单文件时补同级资源目录；
                            base_dir = _common_base(inputs)
      ingest.ingest:694     展开目录/压缩包 → _assemble → _pick_primary
  pipeline.generate:220     复用同一 bundle（不重复读盘）
    ├─ _load_attachments → prototype.parse_prototype / merge_specs → analysis/ui-spec.json
    ├─ _auto_prototypes（无 --attach 时从 bundle.sources 中挑 html）
    ├─ _load_images → images.prepare_from_assets → analysis/image-spec.json
    └─ analysis/prd-bundle.json（ingest.to_json）

Web:  POST /api/v1/prds  (service.upload_prd:443)
      保真落盘（还原目录结构）→ payloads=(name, bytes, "upload", relpath)
      → ingest.ingest(payloads) 立刻解析并把 role/告警回给前端
```
相关文件：`src/prd2tc/` 下的 `ingest.py`、`loader.py`、`prototype.py`、`images.py`、
`llm.py`（`vision_completion`）、`service.py`、`pipeline.py`、`stages.py`；测试：`tests/test_ingest.py`、
`test_prototype.py`、`test_images.py`、`test_service_api.py`。
