# prd2tc 开发交接说明

本文面向远程仓库另一端的开发者，说明当前 `01-AI-TestCaseDesign` 的实现状态、运行方式、数据契约、已知问题和建议开发顺序。除非另有说明，本文所说的项目根目录均为 `01-AI-TestCaseDesign/`。

## 1. 项目定位

prd2tc 将 Markdown/TXT PRD 转换为：

```text
PRD → 需求分析 → 测试方案 → 模块化测试用例 → 追踪矩阵/执行反馈
```

核心原则：

- Markdown 是人工评审视图，JSON 是机器事实源。
- LLM 负责语义抽取和测试设计；程序负责编号、引用、Schema 校验、质量门禁、状态和文件落盘。
- 不让 PRD 模型直接生成真实 CSS/XPath selector，执行侧由 `02-Playwright-Automation` 绑定页面对象。
- CLI 核心保持 Python 标准库 HTTP 调用，不依赖 LangGraph、Redis、FastAPI 或第三方 SDK。
- FastAPI 和 React 仅是可选服务/交互层，不复制 Pipeline 逻辑。

## 2. 当前完成度

### 已完成

- 统一结构化模型：`Requirement`、`Module`、`SourceRef`、`RunManifest`、`RunState`。
- 三阶段 Pipeline：需求分析、测试方案、按模块用例生成。
- 长文档切分、标题路径/字符范围保存、模块并发、模块检查点和 `--resume/--force`。
- `analysis/requirements.json`、`cases/traceability.json`、`run-manifest.json` 原子写入。
- REQ → TC、TC → REQ 双向追踪和 P0 覆盖门禁。
- 动态模块发现；`ecommerce` 仅作为可选 profile，不再作为核心硬编码前提。
- OpenAI、Anthropic、Ollama 统一 LLM 接口，核心使用标准库 HTTP。
- 需求 Diff、增量合并、执行结果导入和示例晋级接口/命令。
- FastAPI 文件工作区：PRD 版本、任务状态、阶段草稿、审批/驳回、取消/恢复、模块重试、WebSocket 事件、产物下载。
- React + Vite + TypeScript 工作台：PRD 编辑预览、运行列表、阶段编辑、用例/追踪查看、Provider 会话配置。
- 正式工作流入口默认使用真实模型；Stub 只能显式开启，并在任务详情标记为 `Stub 离线验收`。
- Web 一键运行：`POST /api/v1/runs {"prdId", "auto": true}` 以 `stage="all"` 一次跑完 分析→方案→用例，跳过逐阶段人工审阅；任务 `auto=true`、各阶段审阅记录标记 `status: "auto" / approvedBy: "one-click"`，失败时定位到具体失败子阶段。
- 可复现性（结构级）：`prd2tc generate --repro`（三阶段温度归零，回归验收基线；`PRD2TC_REPRO=1` 供服务）；成功运行落盘 `reproduce.json` 结构指纹；`prd2tc compare-runs <A> <B>` 对拍（同源零漂移 = 复现，`--fail-on-drift` 可入 CI）。方案见 [reproducibility.md](reproducibility.md)。
- 门禁失败处理文档：[gate-troubleshooting.md](gate-troubleshooting.md)。

### 验证状态

最近一次验证结果：

```text
Python 后端：71 passed
前端 build：通过
前端 Vitest：2 passed
npm audit：0 vulnerabilities
```

测试中出现的 FastAPI `on_event` 是弃用警告，不影响当前行为；后续可迁移到 lifespan。

## 3. 目录导览

```text
src/prd2tc/
├── cli.py          CLI 命令和参数
├── pipeline.py     PipelineRunner、RunResult、恢复逻辑
├── stages.py       三阶段 Prompt 调用、解析、模块并发
├── llm.py          OpenAI/Anthropic/Ollama 客户端
├── stub.py         离线 Stub，仅用于流程验收
├── gates.py        阶段 1/2/3 门禁
├── validator.py    用例字段和 action 枚举校验
├── traceability.py REQ↔TC 矩阵和覆盖门禁
├── chunking.py     长 PRD 切分和检索
├── models.py       结构化数据模型
├── io.py           原子写入、产物渲染和读取
├── service.py      可选 FastAPI 适配层
├── diff.py         需求版本 Diff 与增量合并
├── execution.py    与 02 的执行结果协议
└── profiles.py     generic/ecommerce profile

ui/
├── src/api.ts      Axios API 客户端
├── src/hooks.ts    运行状态和 WebSocket Hook
├── src/components/StageEditor.tsx
├── src/pages/Dashboard.tsx
├── src/pages/PrdPage.tsx
├── src/pages/RunPage.tsx
├── src/pages/Settings.tsx
└── scripts/dev-all.mjs  同时启动 Vite 和 FastAPI
```

不要修改 `ai_test_agent`；它只作为交互设计和开源实现参考。

## 4. 本地启动

### CLI

```bash
pip install -e .
prd2tc generate --prd examples/sample-prd.md --stub --out output
prd2tc validate --dir output/youxuan-mall/v0.3/cases/cases
```

真实模型示例：

```bash
export PRD2TC_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=...
export PRD2TC_MODEL=claude-sonnet-4-5
prd2tc generate --prd examples/sample-prd.md --out output
```

### 交互式工作台

```bash
pip install -e ".[service]"
cd ui
npm install
npm start
```

浏览器访问 `http://127.0.0.1:5173`。`npm start` 会启动：

- Vite：5173
- FastAPI：8000

也可以分别运行 `npm run dev` 和 `npm run dev:backend`。开发代理配置见 `ui/vite.config.ts`。

正式构建：

```bash
cd ui
npm run build
```

然后通过 `create_app(ui_dir="ui/dist")` 托管静态文件。服务端不保存 API Key；Provider 会话只保存在当前服务进程内存中，默认 TTL 为 3600 秒。

## 5. 正式工作流与 Stub 边界

正式运行必须先在工作台“模型配置”页面创建会话。当前 UI 行为：

- 新建运行默认 `stub=false`。
- 没有 `prd2tcSessionId` 时，真实运行会被前端阻止。
- PRD 页面“重新分析”没有会话时直接提示，不再静默降级为 Stub。
- 只有用户显式打开 Stub 开关，才发送 `stub=true`。
- 任务详情字段 `mode` 为 `real` 或 `stub`，列表和详情都会显示运行模式。

Stub 的内容是确定性固定响应，只用于验证审批、重试、检查点、WebSocket 和产物下载；不能用于判断 PRD 对齐质量。

## 6. 服务端 API 摘要

前端 API 基地址为 `/api/v1`。主要接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/prds` | 上传 `.md/.markdown/.txt` |
| GET | `/prds`、`/prds/{id}` | PRD 版本列表和原文 |
| PUT | `/prds/{id}` | 保存新的 PRD 版本，不覆盖历史版本 |
| POST | `/prds/{id}/reanalyze` | 显式创建分析任务，正式模式需要 `sessionId` |
| POST | `/sessions/llm-config` | 创建临时 Provider 会话 |
| POST | `/runs` | 创建运行；参数含 `prdId/stub/jobs/profile/sessionId` |
| GET | `/runs`、`/runs/{id}` | 运行列表和详情 |
| GET | `/runs/{id}/events` | HTTP 事件恢复 |
| WS | `/runs/{id}/events` 或 `/events/ws` | 实时事件 |
| PUT | `/runs/{id}/stages/{stage}` | 保存 analysis/plan/cases 草稿 |
| POST | `/runs/{id}/stages/{stage}/approve` | 批准阶段并触发下一阶段 |
| POST | `/runs/{id}/stages/{stage}/reject` | 驳回阶段并保存原因 |
| POST | `/runs/{id}/modules/{module}/retry` | 只重试失败模块 |
| POST | `/runs/{id}/resume` | 恢复失败/驳回/取消任务 |
| POST | `/runs/{id}/cancel` | 请求取消任务 |
| GET | `/runs/{id}/artifacts` | 产物索引 |
| GET | `/runs/{id}/artifacts/{path}` | 下载单个产物 |

任务状态包括 `pending`、`running`、`awaiting_review`、`rejected`、`succeeded`、`failed`、`cancelled`。阶段审批是显式操作；未批准阶段不能进入下一阶段。

## 7. 持久化目录和事实源

默认工作区为 `.prd2tc-workspace/`，服务工作区结构：

```text
.prd2tc-workspace/
├── prds/       PRD 原文和版本元数据
├── tasks/      每个任务一个 JSON 状态文件
├── events/     <runId>.jsonl 事件日志
├── drafts/     阶段草稿及历史版本
└── outputs/    Pipeline 产物
```

Pipeline 产物通常为：

```text
analysis/requirements.json
analysis/需求分析报告.md
plan/测试方案.md
cases/cases/<MODULE>.json
cases/用例集.md
cases/traceability.json
run-manifest.json
manifest.json        # 旧版本兼容副本
run-state.json
```

`requirements.json` 是需求机器事实源；`run-manifest.json` 记录输入哈希、Prompt 哈希、模型参数、阶段耗时/token/重试、门禁结果和产物索引。所有 JSON 通过临时文件加原子替换写入。

## 8. 门禁和错误处理

门禁失败并不等于 PRD 必须修改。判断顺序：

1. PRD 已包含事实、模型没有输出：先恢复/重新生成或编辑阶段草稿。
2. PRD 确实缺少业务事实：保存新的 PRD 版本，再显式重新分析。
3. REQ/TC 引用、JSON、action、状态等错误：修复结构化结果或重试失败模块。
4. 401、超时、连接拒绝：检查 Provider 会话和网络。

详细规则和示例见 [门禁失败处理手册](gate-troubleshooting.md)。修改门禁逻辑时必须同步更新 `tests/test_gates.py`、`tests/test_validator.py` 和该文档。

## 9. 与 02/03 项目的边界

### 01 prd2tc

- 生成需求、方案、用例。
- 维护 REQ↔TC 追踪和质量门禁。
- 导入执行结果并更新用例副本。

### 02 Playwright Automation

- 将语义 `target` 绑定到页面对象或 selector。
- 执行真实浏览器测试。
- 生成截图、日志、视频等证据。

执行结果契约：

```json
{
  "schemaVersion": "1.0",
  "runId": "...",
  "results": [
    {
      "caseId": "TC-AUTH-001",
      "status": "passed",
      "durationMs": 1234,
      "failureCategory": null,
      "evidence": [],
      "executedAt": "2026-01-01T00:00:00Z"
    }
  ]
}
```

### 03 平台层

负责 CI 触发、报告、通知和组织级质量门禁。平台层应调用 `PipelineRunner` 或服务 API，不要复制阶段逻辑。

## 10. 当前已知限制

- 工作区是单机文件系统，没有账号、权限和数据库；不适合多实例并发部署。
- `cancel` 只在当前模型调用结束或阶段边界生效，不强杀 HTTP 请求。
- FastAPI WebSocket 不是唯一事实源，断线后必须通过 GET 状态和事件恢复。
- 分析阶段对没有显式 REQ 编号的文本会生成确定性编号，但语义仍需人工确认。
- `generic` profile 的动态模块可生成，但部分旧版校验器仍有固定 action/module 枚举；扩展枚举时需要同步 Schema、validator 和测试。
- 服务启动时会把遗留的 `pending/running` 任务归一化为失败，当前不自动恢复正在执行的 HTTP 调用。
- 当前前端测试覆盖基础组件，完整浏览器自动化冒烟仍应补充到 CI。
- FastAPI 使用 `on_event("shutdown")`，后续应迁移到 lifespan API。

## 11. 建议下一步开发顺序

### P0：稳定正式工作流

1. 为真实 Provider 增加服务层集成测试：会话、模型调用、超时、错误脱敏。
2. 增加“门禁失败分类”结构化字段，让 UI 不必只解析错误文本。
3. 为分析/方案/用例草稿增加版本冲突和并发编辑保护。
4. 增加完整 Playwright 浏览器冒烟：上传 PRD → 真实/Stub 运行 → 审批 → 下载产物。

### P1：质量和可维护性

1. 将 `RunPage.tsx`、`Dashboard.tsx` 的单行 JSX 拆分为可测试组件。
2. 为 API 请求补充统一错误类型和重试策略。
3. 将 FastAPI `on_event` 迁移到 lifespan。
4. 增加需求 Diff 的 UI 页面和 modified/removed 用例状态展示。
5. 增加 manifest 脱敏和产物哈希一致性校验的回归测试。

### P2：平台化

1. 稳定 `PipelineRunner(config).generate(request, progress_callback)` 的公开 Python API。
2. 增加执行结果导入后的统计和失败原因分类面板。
3. 再考虑 FastAPI 任务队列、Redis/ARQ、WebSocket 多订阅和外部锁；不要在核心 Pipeline 中提前引入这些依赖。

## 12. 提交前检查清单

```bash
cd 01-AI-TestCaseDesign
python -m pytest -q
python -m compileall -q src
git diff --check

cd ui
npm run build
npm test -- --run
npm audit --audit-level=moderate
```

每个功能改动应同时包含代码、单元测试、Stub 端到端测试、文档和向后兼容说明。不要提交 API Key、临时工作区、真实业务敏感 PRD 或运行日志。
