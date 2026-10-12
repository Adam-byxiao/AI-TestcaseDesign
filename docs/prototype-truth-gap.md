# 原型真值 vs 用例引用：差异报告

> 生成日期：2026-10-12 | 数据来源：`养老Agent后台-prototype-测试内嵌版.html`（48,847 字节）
> 对照对象：运行 `c653dc92` 的 94 条用例
> 复现命令：`python -c "from prd2tc.prototype import parse_prototype; ..."` 或见 `tests/test_prototype.py`
>
> 相关：[case-quality-evaluation.md](case-quality-evaluation.md)（五维评测）·
> [case-quality-fix-strategy.md](case-quality-fix-strategy.md)（生态根因与修复策略）

---

## 0. 为什么需要这份报告

此前评测发现用例引用了 **143 个自拟标识符**、**4 种并存的 target 写法**、
以及**互相矛盾的路由**。当时无法判定这些是「合理实现建议」还是「凭空编造」，
因为作为唯一真值的原型从未参与过生成 —— PRD 正文里 `data-testid` 出现 **0 次**。

现在拿到原型，可以做**逐项判决**。结论是严酷的：

> **143 个标识符中，0 个存在于原型；7 种 URL 路径中，0 个存在（原型是单页应用，无路由）。**

这意味着此前所有用例的 `target` **没有一个能被真实执行**，
而这一点在缺少原型时**无法被门禁发现**（`validator` 只判 target 非空）。

---

## 1. 原型真值摘要

| 维度 | 真值 |
|---|---|
| 导航页 | `history`（语音对话联系历史）、`config`（智能体配置）、`knowledge`（知识库配置） |
| 页签 | `main`（主提示词）、`tools`（工具）、`skills`（业务场景）— **与 PRD §6.1.1 完全一致** |
| URL 路由 | **不存在**。导航为 `function go(p){page=p;render()}`，纯 JS 状态切换 |
| 真实锚点 | **91 个**（20 个 `id` + 70 个 `class` + 1 个 `data-*`） |
| `data-testid` | **0 个** |
| 弹窗 | 10 个，按钮**均为 1–2 个** |
| 提示/校验文案 | 31 条 |
| 表格 | 4 张（含「文档名称｜解析状态｜片段数量｜更新时间｜启用｜操作」） |
| 持久化 | `localStorage` 0 次、`fetch` 0 次 → 纯原型模拟（与 PRD §7 一致） |

**20 个真实 id**：`app` `chat` `count` `docFile` `docName` `fullText` `modal` `nameCount`
`nav` `promptEditor` `publishState` `retrieveQuery` `retrieveResults` `saveAll` `saveState`
`selectedFile` `skillName` `skillText` `testInput` `toast`

---

## 2. 判决一：143 个标识符，0 个真实

| 判定 | 数量 |
|---|---:|
| 用例引用的去重标识符 | **143** |
| 存在于原型契约中 | **0** |
| **编造** | **143（100%）** |

被引用最多的编造标识符（括号内为引用它的用例数）：

| 编造标识符 | 用例数 | 原型中的真值 |
|---|---:|---|
| `tab-tool` | 21 | 页签由 `tab='tools'` 控制，无此元素 |
| `editor-main-prompt` | 19 | 真实为 `textarea#promptEditor` |
| `page-kb-list` | 15 | 真实为导航页键 `knowledge` |
| `khtest-result-list` | 12 | 真实为 `#retrieveResults` |
| `field-dropzone` | 10 | 真实为 `class="file-drop"` |
| `kb-upload-modal` | 10 | 真实为 `#modal` |
| `tool-item-weather` | 10 | 真实为工具列表项（无稳定 id） |
| `kb-row-status` | 9 | 真实为表格 `<td>` 第 2 列 |
| `khtest-run-btn` | 9 | 真实为按钮文案「测试」 |
| `khtest-scope-select` | 9 | 真实为 `<select>`（无 id） |

> 编造并非乱来：`tab-tool` 对应真实页签 `tools`、`editor-main-prompt` 对应
> `#promptEditor` —— 模型**语义上理解正确**，只是**没有真值可依**，只能自造命名。
> 这印证了此前的判断：**契约要求了输入侧不存在、且无供给通道的事实。**

---

## 3. 判决二：7 种 URL 路径，0 个真实

原型为单页应用，**不存在任何 URL 路由**（`location.hash`/`history.pushState` 均无）。

| 用例中的路径 | 出现次数 | 判定 |
|---|---:|---|
| `/agent-config` | 43 | ❌ 原型中不存在 |
| `/agent/knowledge-base` | 16 | ❌ 不存在 |
| `/knowledge/list` | 14 | ❌ 不存在 |
| `/agent/khtest` | 8 | ❌ 不存在 |
| `/20` | 3 | ❌ 不存在（是 `maxlength=20` 的误读） |
| `/agent/config` | 2 | ❌ 不存在 |
| `/agent/test` | 1 | ❌ 不存在 |

**真值**：页面键为 `history` / `config` / `knowledge`。

**并且原型中同一个页面被用了两套地址** —— `/agent-config`（43 次）与
`/agent/config`（2 次）互不相同，正是 §7.2 所述「两批独立生成」的旁证。

---

## 4. 判决三：弹窗「三选项」是臆造

PRD §6.1.3 原文只说「**弹窗提醒**」，未定义选项。用例 `TC-AGENT-012/013/014/015`
生成了完整三选项分支（保存 / 放弃 / 取消）。**原型真值否证了它**：

| 弹窗标题 | 真值按钮 |
|---|---|
| **未保存修改** | **`['关闭']`** ← 只有 1 个 |
| 确认发布配置 | `['取消', '确认发布']` |
| 删除业务场景 | `['取消', '确认删除']` |
| 删除文档知识 | `['取消', '确认删除']` |
| 新增业务场景 | `['取消', '保存']` |
| 放大编辑 | `['取消', '保存到草稿']` |
| 更新/添加文档知识 | `['取消', '保存']` |
| 已发布内容预览 / 命中测试 / 通话详情 | `['关闭']` |

**原型中所有弹窗按钮均为 1–2 个**，从未出现三选项。

更关键的是「未保存修改」弹窗的**真实语义与用例完全相反**：

```
原型原文：停止编辑后自动保存全部草稿；您也可点击顶部"保存全部草稿"立即保存。
          切换内容不会丢失输入。
```

即：**它是信息提示，不阻塞切换**（`onclick="closeModal()"`）。
而用例 `TC-AGENT-012` 断言「弹窗出现后停留在原页签」，`TC-AGENT-014` 断言
「选择放弃后内容回滚」—— 这些行为在原型的真实交互中**不存在**。

真正阻断「未保存时测试」的机制在原型里是另一处：
`function sendTest(){ if(dirty) return toast('请等待草稿保存完成'); ... }`
—— 文本提示，非弹窗。

---

## 5. 判决四：预期文案 0% 命中契约原文

原型有 **31 条**真实提示文案（`文件大小不能超过 1MB`、`文档名称已存在`、
`请上传 txt、docx 或 xlsx 文件`、`文档解析成功后才能启用`…）。

| 指标 | 数值 |
|---|---:|
| 用例预期总数 | 175 |
| 能匹配契约原文的 | **0（0%）** |

模型在**不知道这些原文存在**的情况下，只能自行撰写近义描述
（如用例写「超出大小提示」而非原文「文件大小不能超过 1MB」）。
这直接损害了 `expected` 的可验证性：**断言应对照真实文案，而非模型的转述**。

---

## 6. 门禁按契约校验的结果

新增的 `validate_targets(cases, spec)` 对 94 条用例报出：

| 错误类型 | 数量 |
|---|---:|
| 编造标识符 | **417** |
| 编造 URL 路由 | **84** |
| **合计** | **501** |

示例（真实输出）：

```
[用例 TC-AGENT-001] target 出现 URL 路径 `/agent-config`，但原型是单页应用、
    不存在 URL 路由；`goto` 应使用页面键 ['config', 'history', 'knowledge']
[用例 TC-AGENT-001] target 引用了契约中不存在的标识符 `tab-main-prompt`
    （原型仅有 91 个真实锚点）；请改用真实 id/class 或去掉该标识符
```

> 这不是「门禁太严」：每一条都对应一个**无法执行**的步骤。
> 修复前这些缺陷会 100% 流向用户，而用户拿不到原型对照时也无从发现。

---

## 7. 已实施的修复

| 层 | 改动 |
|---|---|
| **新能力** | `src/prd2tc/prototype.py`：零依赖原型解析（静态 HTML + JS 模版 + 状态 + 导航 + 弹窗 + 文案）→ **UI 契约真值**，含确定性输出 |
| **新增入口** | `prd2tc generate --attach <原型.html>`（可重复）；解析摘要与 `sha256` 写入 `run-manifest.json`；契约落盘 `analysis/ui-spec.json` |
| **提示注入** | 契约渲染为紧凑清单注入**阶段 3 system 前缀**（跨批次共享、命中缓存）：真实 id/class、页面键、弹窗及其**全部按钮**、真实文案，并明确「不得编造标识符与路由」 |
| **门禁强校验** | 批次门禁追加 `validate_targets()`：编造标识符、无路由时的 URL、goto 目标不存在 → 报错回灌，由既有重试机制要求模型改正 |
| **锚点稳定** | `uiSpecHash` 纳入 `configHash` → **原型变更使旧检查点失效**，避免复用按旧标识符生成的用例 |
| **失败降级** | 附件不可解析时**只告警不中断**（原型是增强输入，非硬依赖） |
| **回归防护** | `tests/test_prototype.py` 34 个测试；**5 项变异测试全部被捕获** |

---

## 8. 复现与验收

```bash
# 解析原型并查看契约摘要
prd2tc generate --prd <prd.md> --attach <原型.html> --stub --out /tmp/x
# → ✔ 已解析原型 …：页面 ['history','config','knowledge']、页签 ['main','tools','skills']、
#    锚点 91、文案 31、URL 路由 无

# 契约落盘
cat /tmp/x/<product>/<version>/analysis/ui-spec.json
```

**建议的端到端验收**：用**真实模型**重跑同一 PRD 并附上原型，
对比修复前的三项指标：

| 指标 | 修复前 | 期望 |
|---|---:|---|
| 编造标识符 | 143 / 143（100%） | 0 |
| 编造 URL 路由 | 7 种 / 84 处 | 0 |
| 预期命中契约原文 | 0 / 175（0%） | 显著提升 |
| 弹窗臆造选项 | 三选项（原型仅 1 个按钮） | 0 |

> 注：契约校验在**有** `--attach` 时才生效；纯文本 PRD 场景保持原行为（已测试保证不误报）。
