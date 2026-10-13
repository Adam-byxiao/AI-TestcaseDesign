# 服务模式与前端工作台

> 本文只讲**适配层**：`src/prd2tc/service.py`（FastAPI）与 `ui/`（React 工作台）。
> 编排与生成逻辑在 `pipeline.py`，见 [pipeline.md](pipeline.md)。
> 文中形如 `src/prd2tc/service.py:317` 的引用是**行号锚点**，代码改动后请以符号名为准。

## 1. 服务模式定位

### 1.1 可选依赖，不污染核心 CLI

核心 CLI 的定位是「零第三方依赖」（`pyproject.toml` 的 `description`）。服务模式所需的一切都放在可选分组里：

```toml
[project.optional-dependencies]
service = ["fastapi>=0.110", "uvicorn>=0.29", "websockets>=12", "python-multipart>=0.0.9"]
```

`websockets` 不是可选项而是**必需**：缺它时 uvicorn 无法完成 WebSocket 升级，实时推送会静默退化为 10 秒轮询（`README.md:79`、`pyproject.toml:26`）。

`create_app()` 用**函数内 import** 兜住缺依赖的情况，并把安装命令写进异常：

```python
except ImportError as exc:
    raise RuntimeError("服务模式需要安装: pip install 'prd2tc[service]'") from exc
```
（`src/prd2tc/service.py:248-252`）

**为什么这么设计**：`service.py` 顶层不 import fastapi，所以 `import prd2tc.service` 在纯净环境里不会因为缺依赖而在导入期崩溃，错误信息也直接给出可复制的修复命令 —— 而不是让用户看到一长串 `ModuleNotFoundError` 栈。

### 1.2 启动方式

```bash
pip install '.[service]'
uvicorn 'prd2tc.service:create_app' --factory --host 127.0.0.1 --port 8000
```
（`README.md:157-160`）

开发期一条命令同时起两端：

```bash
cd ui && npm install && npm start      # = npm run dev:all
```
（`ui/package.json` 的 `start` → `dev-all.mjs`）

`ui/scripts/dev-all.mjs` 用 `spawn(..., {stdio:'inherit'})` 起两个子进程（`dev-all.mjs:11-13`）：FastAPI 走 `python -m uvicorn prd2tc.service:create_app --factory --host 127.0.0.1 --port 8000`（8000），Vite 走 `npm run dev -- --host 127.0.0.1`（5173）。

它把仓库 `src/` 注入 `PYTHONPATH`（`dev-all.mjs:9`），因此**无需先 pip install** 即可开发；`SIGINT/SIGTERM` 会一并结束两个子进程（`dev-all.mjs:14-15`）。FastAPI 启动失败时会补一句 `pip install -e ".[service]"` 的提示（`dev-all.mjs:11`）。

开发期前端通过 Vite 代理访问后端，**且代理带 `ws:true`**，否则 WebSocket 无法升级：

```ts
server:{port:5173,proxy:{'/api':{target:'http://127.0.0.1:8000',ws:true}}}
```
（`ui/vite.config.ts`）

### 1.3 工厂参数与静态托管

签名即 `create_app(config=None, *, state_dir=".prd2tc-workspace", max_workers=2, ui_dir=None)`（`src/prd2tc/service.py:246-247`）。

- `state_dir`：工作区根目录，默认当前目录下 `.prd2tc-workspace`。
- `max_workers`：`ThreadPoolExecutor` 线程数，`max(1, max_workers)`；**默认 2**，即最多同时跑 2 个运行任务（阶段内部的模块并发由 `jobs` 控制，两者是不同层级）。
- `ui_dir`：前端构建产物目录；缺省为仓库内 `ui/dist`。
- `config`：基础 `Config`，会话值会以 `dataclasses.replace` 叠加在它之上（`service.py:343-345`）。

存储实例挂在 `app.state.store` 上（`service.py:253-255`），这是排障入口：可以直接检查 `app.state.store.sessions` 判断某个 LLM 会话是否还活着（测试也依赖这条路径，`tests/test_service_api.py:836-840`）。

静态托管只在目录存在时挂载（`service.py:1025-1035`）：

- `/assets` 挂 `StaticFiles`，交由浏览器按正常资源缓存规则处理；
- 兜底路由 `GET /{full_path:path}`：命中真实文件就返回该文件，否则返回 `index.html`，从而支持 React Router 的 `/runs/:id` 直接刷新。
- 该兜底用 `static_dir in candidate.parents` 做目录穿越防护（`service.py:1034`），因为 `full_path` 是用户可控的。

生产部署即 `cd ui && npm run build` 后把 `ui/dist` 传给 `create_app(ui_dir="ui/dist")`（`README.md:179`）。产物验收测试见 `tests/test_service_api.py:1051-1058`（`/` 与 `/runs/new` 均应 200 且含 `id="root"`）。

### 1.4 遗留 API 与 SPA 的第二次兜底

旧客户端仍在用的两个路由是 `POST /runs`（`body.prd` 必须指向可读文件，直接构造请求入队）与 `GET /runs/{run_id}`（任务不存在时**再检查一次 `index.html`**，存在就返回 SPA 而不是 404）（`service.py:1010-1023`）。

**为什么 `GET /runs/{run_id}` 要兼职 SPA 兜底**：它注册在前缀 `/{full_path:path}` **之前**，会先匹配 `/runs/xxx`。若不做这层兜底，用户直接刷新 `/runs/<id>` 就会命中这个路由并拿到 404，永远看不到 SPA 兜底路由。这是一个先注册者优先的路由顺序陷阱，代码里用「同一份 index.html 检查两次」绕过去。

## 2. 状态模型

### 2.1 工作区目录布局

根目录（默认 `.prd2tc-workspace/`）在 `WorkspaceStore.__init__` 中创建 `tasks/`、`prds/`、`events/`（`service.py:155-156`）；`drafts/` 与 `outputs/` 按需创建（`service.py:279`、`service.py:720`）。实测布局：

```
.prd2tc-workspace/
├── prds/prd-<12hex>.json                      # 上传元数据（meta，含 files[] 清单）
├── prds/prd-<12hex>/                          # 保真落盘的原始文件，按相对路径还原层级
├── tasks/<taskId>.json                        # 任务状态（唯一事实源）
├── events/<runId>.jsonl                       # 追加式事件流
├── drafts/<runId>/analysis.json               # 最新草稿（apply_draft 读这个）
├── drafts/<runId>/analysis/draft-0001.json    # 历史版本，逐次递增
└── outputs/<taskId>/                          # pipeline 产物根（run-manifest.json / run-state.json / 三个阶段目录）
```

**为什么 PRD 文件要单独放进 `prds/<id>/` 子目录**：`service.py:576-579` 的注释记录了原因 —— 若新版本扁平落在 `prds/` 根下，会与「旧目录 + 新扁平副本」并存，目录结构难以理解。上传与另存新版本两处必须一致。

**为什么要在磁盘上还原上传的目录层级**：`_safe_relpath_parts` 的注释给出了实测事故 —— 若把上传文件全部拍平，`图片和附件/rag-service.md` 会与 PRD 同级，摄取层赖以判定主文件的 `depth` 全部退化为 0，层级线索失效（`service.py:108-125`）。清洗规则是逐段做过 `_safe_filename`，丢弃 `.`/`..`/盘符/含冒号的段。

### 2.2 任务 JSON 字段

任务文件是全量重写（`save_task` → `_json` 读旧值 → `dict.update` → `atomic_write_text`，`service.py:199-202`）。实测某任务文件的字段集合：

| 字段 | 取值/结构 | 写入位置 |
|---|---|---|
| `taskId` / `createdAt` / `updatedAt` | 字符串；`updatedAt` 每次 save 刷新 | `service.py:202` |
| `status` | `pending`/`running`/`awaiting_review`/`succeeded`/`failed`/`cancelled`/`rejected` | 各处 `save_task` |
| `currentStage` | `queued`/`analysis`/`plan`/`cases`/`done`/`cancelled` | `service.py:360,428,638` |
| `stageStates` | `{analysis,plan,cases} → pending/running/awaiting_review/approved/succeeded/failed/rejected` | `service.py:359,762,835-839` |
| `percent` | 阶段级粗粒度 33/66/100（**不是**细粒度进度） | `service.py:428` |
| `review` | `{stage → {status, approvedBy, approvedAt, contentHash, reason}}` | `service.py:427,762,774` |
| `modules` | `{模块名 → succeeded/running/failed/skipped}`，来自 `run-state.json` | `service.py:404-405,428` |
| `qualityGate` | `{stage → gate}`，来自 `run-manifest.json` | `service.py:407,418,428` |
| `tokens` | `{total, stages:{name:{tokens, moduleTokens}}}` | `service.py:409-411` |
| `request` | 序列化后的 `GenerateRequest`（`prd`/`out`/`product`/`version`/`stub`/`jobs`/`profile`/`max_gate_retries`/`allow_gate_fail`/`no_gate`/`attachments`/`image_dirs`/`fetch_images`/`analyze_images`） | `service.py:287-305,638` |
| `sessionId` | 上次使用的 LLM 会话 ID（**可能已失效**，见 §4） | `service.py:638,766,786,841,887` |
| `mode` / `llm` / `auto` | `stub`\|`real`；`{provider,model,apiBase}`（apiBase 已脱敏）；一键运行标记 | `service.py:638,686-687` |
| `output` / `runId` | 产物根目录绝对路径 / pipeline 生成的运行 ID | `service.py:428` |
| `errors` / `error` | 错误列表 / 单条错误（已 `redact`） | `service.py:437-440` |
| `lastEventId` | 每条事件写入时同步刷新（`retryModule` 仅模块重试时写入） | `service.py:211,786` |
| `metrics` | **不落盘**，只在 `GET /runs/{id}` 时从内存注入 | `service.py:695-698` |

`request.prd` 的序列化踩过一次坑：原先写 `str(req.prd)`，多文件列表会被字符串化成 `"[WindowsPath('...'), WindowsPath('...')]"`，阶段一用内存里的原始请求故能成功，阶段二从 task.json 重建时把这串文本当路径，报「输入不存在」（`service.py:287-294`）。现在用 `_encode_prd`/`_decode_prd` 双向转换，并保留 `_LEGACY_PRD_RE` 还原旧记录，好让**已经失败的任务仍能恢复**，不必让用户重新上传（`service.py:47-65`）。

**为什么 `request` 要持久化 `attachments`/`image_dirs`/`fetch_images`**：这些选项不在 URL 或会话里，阶段 2/3 从 task.json 重建请求时会丢失，导致 `--attach` 的 UI 契约（原型 HTML）在后续阶段凭空消失（`service.py:300-305`）。

### 2.3 事件 JSONL 与「WebSocket 不是唯一事实源」

事件写入是**整文件重写**：先 `path.read_text(...)` 读出全部旧内容，再 `atomic_write_text(path, previous + json.dumps(event) + "\n")` 整体写回（`service.py:205-211`）。

事件类型（`type`）目前有：`run_started`、`stage_progress`、`heartbeat`、`stage_ready`、`run_finished`、`run_failed`、`run_cancelled`、`draft_saved`、`stage_approved`、`stage_rejected`、`stage_regenerate`，以及来自 pipeline 的结构化事件（`stage_started`/`batch_started`/`module_finished` 等，经 `on_event` 转发，`service.py:374-383`、`tests/test_realtime.py:55-64`）。

「WebSocket 不是唯一事实源」体现在三处：

1. 任务状态与事件都**先落盘**，WS 只是把 `events/<runId>.jsonl` 里尚未发送的行转发出去（`service.py:987-990`）；
2. `GET /api/v1/runs/{run_id}` 直接带回 `events`（末 100 条）与 `metrics` 快照，刷新页面即可恢复（`service.py:695-708`）；
3. `useRun` 在建立 WS 之前先做一次 HTTP 快照，**额外挂 10 秒定时轮询**，并在断开时按指数退避重连、上限 10s（`ui/src/hooks.ts:19,33,36`）。

**为什么 WS 不能是唯一事实源**：`websockets` 未安装、代理未开 `ws:true`、服务重启、终态连接关闭后重连等路径都会让 WS 静默失效。若前端只信 WS，这些情况下进度面板会永久停在旧值且没有任何提示；以落盘状态 + 轮询为底，WS 只是「更快的那条路」。

前端还有一个细节：WS 路径用 `/events/ws` 而不是 `/events`（`hooks.ts:23-24`）—— Starlette 中 `GET /runs/{id}/events` 会**遮蔽**同路径的 WebSocket 升级，服务端因此把两个路径都注册上（`service.py:979-980`）。

### 2.4 为什么实时指标走内存（O(n²) 的代价）

`WorkspaceStore.event()` 每写一条事件都要把整个 JSONL 读出来再重写。若按 token 落盘（一次运行可产生上万条 delta），写入量随事件数线性增长 → 总代价 O(n²)。`metrics.py` 的模块 docstring 把这条列为第 1 条设计约束（`src/prd2tc/metrics.py:5-7`）：**绝不落盘事件**，实时指标只存内存，由 WS 推快照。

因此有两套数据：

| | 事件 JSONL | `LiveMetrics`（内存） |
|---|---|---|
| 内容 | 阶段/模块/批次的生命周期里程碑 | Token、首 token 延迟、静默时长、输出尾巴 |
| 频率 | 每次状态变化（含 5s 心跳） | 每个流式 delta 累加，快照按 500ms 限流 |
| 生命周期 | 落盘，可回溯 | `store.live[taskId]`，运行结束即淘汰 |
| 崩溃恢复 | 完整 | 仅批次边界覆盖写 progress.json |

热路径只做整数累加，token 估算推迟到 `snapshot()`（`metrics.py:94-126,185-262`）；`store.live[task_id] = runner.live` 在运行开始登记、`finally` 中 `store.runners.pop`（`service.py:363,403`）。

**为什么快照要限流**：WS 循环每 0.5s 读一次（`service.py:1006`），而 `snapshot()` 内部按 `push_interval_ms`（默认 500ms，来自 `config.stream_push_ms`）判重，避免高频调用把大对象序列化开销堆到流式热路径上。

## 3. 端点清单

统一前缀 `/api/v1`。路径参数先过 `require_id`（仅允许 `[A-Za-z0-9-]+`，否则 400「非法 xxx」，`service.py:259-261`）。

### 3.1 PRD 上传与版本管理

| 方法 | 路径 | 用途 | 请求 / 响应要点 |
|---|---|---|---|
| POST | `/prds` | 上传 PRD（单/多文件、多格式、文件夹） | multipart：`files`（推荐）与 `file`（兼容）取并集，须声明为 `List[UploadFile]`（用 `Any` 会 422）。上限 400 文件 / 单文件 64MB / 总量 256MB。每个文件**保真落盘**（原始字节，不文本化）。响应 `{prdId, filename, path, dir, files[], primary, contentDigest, bundle, warnings, version, sha256, chars, createdAt}` |
| GET | `/prds` | 列出版本 | 按 `createdAt` 倒序 |
| GET | `/prds/{prd_id}` | 取原文 + 版本历史 | 追加 `content`（UTF-8 文本）与 `versions[]`（`filename` 相同的所有记录） |
| PUT | `/prds/{prd_id}` | 保存新版本 | body `{content}`；生成**新 prdId**，写 `parentPrdId`，版本号 `_next_version` 递增，复制上一版的图片/附件，重建 `files[]` 与 `contentDigest` |
| POST | `/prds/{prd_id}/reanalyze` | 用当前配置重跑阶段一 | body 同 `POST /runs`（`stub`/`jobs`/`profile`/`sessionId`/`parentRunId`）；返回新建任务 |

实现位置：`service.py:443-556`、`557-558`、`559-566`、`567-626`、`628-639`。

三条关键设计：

- **落盘保真而非文本化**（`service.py:454-456`）：图片与 `.docx` 的二进制一旦被当文本处理就毁了，格式判定交给 `ingest`。
- **上传后立刻用摄取层真实解析一次**（`service.py:504-519`）：把「哪个文件是正文、识别出多少资产、有哪些告警」当场回给前端，避免用户上传完才发现格式不支持。`path` 必须是主文件**在磁盘上的实际相对路径**（可能位于还原出的子目录，如 `我的PRD/PRD.md`）；只用 basename 会让后续运行找不到显式主文件。
- **按内容去重（第二道防线）**（`service.py:521-536`）：文件集合的 sha256（排序后）完全相同即复用已有记录并返回 `deduplicated: true`。前端曾因 antd 逐文件回调把一次文件夹上传发成 N 次请求，列表里堆了 8 条同名同版本记录；仅修前端不足以防御「连点两次」「脚本重复调用」，故在服务端按内容去重。`PUT /prds/{id}` 是用户显式行为，**不**走此路径。

`PUT` 的三个易错点（`service.py:576-624`）：新版本必须在自己的目录里（与上传一致）；主文件必须写回**旧的相对位置**，否则会出现「旧副本 + 新扁平副本」两份 PRD；上一版的其余文件（图片/附件/子目录）必须复制过来，否则新版本目录只剩主文件、后续运行读不到配图，而 `files` 元数据仍声称有 N 个文件。

### 3.2 运行生命周期

| 方法 | 路径 | 用途 | 请求 / 响应要点 |
|---|---|---|---|
| POST | `/runs` | 创建运行（**202**） | body：`prdId`（必填，否则 400）、`stub`、`jobs`、`profile`、`sessionId`、`auto`、`product`、`version`、`maxGateRetries`、`allowGateFail`、`noGate`。`sessionId` 无效 → 401 且**不入队**。`auto=true` 时内部用 `stage="all"` 且关闭逐阶段审阅。响应为落盘后的任务对象 |
| GET | `/runs` | 列出全部运行 | 按 `updatedAt` 倒序 |
| GET | `/runs/{run_id}` | 运行详情 | 任务对象 + 运行中注入 `metrics` + 有产物时注入 `stageMeta`/`product`/`version` + `events`（末 100 条） |
| GET | `/runs/{run_id}/events` | 全量事件 | 返回 JSONL 解析出的数组 |
| POST | `/runs/{run_id}/cancel` | 取消 | 先 `runner.cancel()` 真正止损，再落 `cancelled` 并发事件 |
| POST | `/runs/{run_id}/resume` | 恢复失败运行 | 由 `failedStage \|\| currentStage` 推出阶段号；推不出 → 409。可带 `sessionId` |
| POST | `/runs/{run_id}/modules/{module}/retry` | 重试单个失败模块 | 模块不存在 → 404，非 `failed` 状态 → 409。可带 `sessionId` |

实现位置：`service.py:675-687`、`688-689`、`690-708`、`709-710`、`787-793`、`877-887`、`777-786`。

**取消为什么要「先 cancel 再落盘」**：`runner.cancel()` 置的是流式回调逐块检查的取消标志（`metrics.py:306-316`），命中后立即停止接收响应，这才是真正止损；只改 `status` 字段的话运行会继续烧 token。同时 `progress()` 回调加了防线：已取消时不得把状态改回 `running`（`service.py:366-369`），否则取消会被后续进度事件覆盖。

**`GET /runs/{run_id}` 为什么要把 `stageMeta` 和 `product/version` 一并带出**（`service.py:699-707`）：前者是三个阶段能**分开呈现**的前提；后者只存在于运行清单里，而导出对话框需要它们生成有意义的默认文件名。

**重启即失败**：`WorkspaceStore.__init__` 会把上次遗留的 `pending`/`running` 任务改写成 `failed`，`failedStage` 记下原 `currentStage`，错误信息为 `service restart before task completion`（`service.py:157-162`）。这是为了让「恢复」有明确入口 —— 而不是让任务永远停在 `running`。旧 `TaskStore` 有同样逻辑（`service.py:136-140`）。

### 3.3 阶段审阅

| 方法 | 路径 | 用途 | 请求 / 响应要点 |
|---|---|---|---|
| PUT | `/runs/{run_id}/stages/{stage}` | 保存阶段草稿 | `stage ∈ {analysis,plan,cases}`，否则 400。body 整体做哈希存为草稿内容，写入 `draft-NNNN.json` 与最新 `draft.json` 两份；返回 `{schemaVersion,runId,stage,draftVersion,content,savedAt,contentHash}` |
| POST | `/runs/{run_id}/stages/{stage}/approve` | 批准并推进 | 仅 `status=awaiting_review` 且 `currentStage==stage` 时可批（否则 409）。body 可带内容（等价于「先存草稿再批准」）+ `sessionId`；`sessionId` 会先从草稿体中摘掉 |
| POST | `/runs/{run_id}/stages/{stage}/reject` | 驳回 | body `{reason}` 非空，否则 400；落 `status=rejected` 与 `review[stage]={status:rejected,reason,...}` |
| POST | `/runs/{run_id}/stages/{stage}/regenerate` | 单阶段重跑 | 见 §6 |

实现位置：`service.py:712-725`、`750-766`、`768-775`、`803-854`。

批准流程（`service.py:750-766`）依次做四件事：解析会话 → 若带内容则存草稿 → `apply_draft` 把草稿**应用到产物目录** → 记录审阅并推进。`apply_draft` 是「草稿 → 产物」的唯一通道（`service.py:727-748`）：

- `analysis`：写 `需求分析报告.md` 与 `requirements.json`，结构化需求缺 `id`/`module` → 400；
- `plan`：写 `测试方案.md`，缺 `markdown` → 400；
- `cases`：逐条 `validate_case` + `build_traceability`/`validate_traceability`，**有任何错误直接 400 并回传错误数组**，不写入半成品。

**为什么 `sessionId` 必须先摘掉**（`service.py:755-758`）：`save_draft` 对 body 整体做哈希并原样存为草稿内容，混进去会污染草稿与 `contentHash`，进而影响「已审阅内容」的指纹比对。回归测试见 `tests/test_service_api.py:434-454`。

批准阶段一/二后自动进入下一阶段：`states[next_name]="pending"`、`status=pending`，并以 `request_from(task["request"], next_stage, True)` 重建请求后入队（`service.py:765-766`）；批准 `cases` 则直接 `succeeded` + `currentStage=done`（`service.py:764`）。

### 3.4 产物与导出

| 方法 | 路径 | 用途 | 请求 / 响应要点 |
|---|---|---|---|
| GET | `/runs/{run_id}/artifacts` | 产物清单 | 递归列出产物文件，**排除 `.checkpoints` 目录**；路径统一为 `/` 分隔；含 `size` 与 `contentType` |
| GET | `/runs/{run_id}/artifacts/{artifact_path:path}` | 读取单个产物 | `.json` → JSON 响应；`.xlsx/.xmind/.zip/.png/.pdf` → `FileResponse` 下载；其余按 UTF-8 文本返回。路径必须落在产物根内且是文件，否则 404 |
| GET | `/exports/{run_id}/{fmt}` | 按需生成并下载导出文件 | `fmt ∈ {excel,xmind}`（否则 400）；查询参数 `filename`、`xmind_mode ∈ {case,full,module}`、`sheet_name`、`modules`/`priorities`/`tags`/`keyword` |

实现位置：`service.py:888-896`、`897-908`、`943-978`。

- **为什么要按扩展名区分读取方式**（`service.py:903-907`）：二进制产物用 `read_text` 会抛 `UnicodeDecodeError`。这是引入导出功能时暴露的连带问题（回归测试 `tests/test_service_api.py:1022-1041`）。
- **`filename` 必须清洗**：它直接进 `Content-Disposition`，未清洗时 `"` 或换行可造成**响应头注入**，`../` 可能被部分客户端当路径。`_download_name` 只取基名、剔除控制字符与引号、强制正确扩展名，清洗后为空则回退默认名（`service.py:68-83`）。
- **`mimetypes` 不认识 `.xmind`**，需手动注册 `application/vnd.xmind.workbook`，否则 artifact 接口给出的 `Content-Type` 是错的（`service.py:129-130`）。
- 导出目录固定在运行产物内（`out=None, root=output`），不接受外部路径参数，因此**无需 `_safe` 校验：无穿越面**（`service.py:952`）。副作用是导出文件会累积在 `outputs/<taskId>/` 下，实测该目录里同时存在 `用例集.xlsx` 与按筛选命名的子集导出。
- XMind 节点数超限时会**自动降级**为 `module` 模式并告警，而不是直接失败；`xmind_mode` 已在服务端校验，非法值 400（`src/prd2tc/export/xmind.py:192-215`、`service.py:963-964`）。

### 3.5 分面统计

| 方法 | 路径 | 用途 | 请求 / 响应要点 |
|---|---|---|---|
| GET | `/runs/{run_id}/cases/facets` | 三维度可选值 + 实时数量 | 查询参数 `modules`/`priorities`/`tags`（重复键）、`keyword`、`ignore` |

实现位置：`service.py:920-941`，语义在 `src/prd2tc/casefilter.py`。

- 语义唯一规则：**维度内取并集（OR），跨维度取交集（AND）**（`casefilter.py:1-13`）。选多个模块是「这几个模块都要」；「单模块中的回归用例」是跨维度组合。若维度内也做 AND，「多选模块」将永远选不出用例。
- `ignore` 指定统计哪个维度时**忽略它自身**的条件，传 `*` 表示每个维度各自忽略自身（分面搜索的标准语义），前端一次请求即可取全（`casefilter.py:160-215`）。否则选了「模块=KB」后优先级列表只剩 KB 里的项，用户看不到「切到 P0 还剩多少条」。
- 非法 `ignore` → 400；运行或用例产物不存在 → 404「该运行尚无用例产物」（`_cases_for`，`service.py:910-918`）。
- 响应含 `{total, matched, modules[], priorities[], tags[], filter}`，`filter` 来自 `casefilter.summarize`（`filtered`/`excluded` 等）。

### 3.6 WebSocket

| 协议 | 路径 | 用途 |
|---|---|---|
| WS | `/runs/{run_id}/events` 与 `/runs/{run_id}/events/ws` | 推送事件；`?metrics=1` 时**额外**推送 `run_metrics` 快照 |

实现位置：`service.py:979-1006`。循环每 0.5s 做三件事：发未发送的事件（按 `events[sent:]` 下标推进）、按需推指标快照、终态时关闭连接。

三个刻意的设计：

1. **`?metrics=1` 才推指标**：旧客户端/旧测试不会收到未知消息类型。回归测试 `tests/test_realtime.py:102-119` 断言不订阅时绝不出现 `run_metrics`。
2. **终态也必须先推一帧指标再关闭**：短运行可能在本连接建立前就结束，若直接 close，客户端一帧都收不到、实时面板永远空白。所以 `snapshot(force=terminal or not metrics_pushed)` 在终态强制出帧，且关闭条件写成 `terminal and (not wants_metrics or metrics_pushed)`（`service.py:996-1005`）。回归测试 `tests/test_realtime.py:138-161`。
3. **`run_metrics` 不是事件**：它没有 `eventId`，前端必须把它路由到独立的 `metrics` 状态，绝不能塞进 `events`（`hooks.ts:28-29`）。

服务端另有一条**心跳**线程（每 5s）写一条 `heartbeat` 事件，消息形如 `静默 12s · 已用 340s`，状态进入终态即退出（`service.py:385-397`）。

## 4. LLM 会话模型（运维必读）

会话是**服务端内存态**：以 `secrets.token_urlsafe(24)` 生成 ID，存为 `sessions[sid] = (到期时间, 配置快照)`，`ttl` 默认 3600s；`get_session` 发现过期即 `pop` 掉并返回 `None`（`service.py:217-222`）。

| 事实 | 后果 |
|---|---|
| 存于 `WorkspaceStore.sessions` 字典 | **随服务重启失效**（进程内存没了） |
| TTL 3600s（1 小时），`llm-config` 响应里回显 `expiresIn: 3600` | 保存后 1 小时未操作即失效 |
| API Key 只在进程内存，不落盘 | 任务 JSON 里只有 `sessionId`；响应与日志均不含 Key |
| `redact()` 用当前所有会话的 Key 做字符串替换 | 异常文本落盘前被脱敏 |

安全取舍：Key 不落盘意味着**不存在静态泄露面**，代价是每次服务重启后都要重新配置。验收测试 `tests/test_service_api.py:114-121` 遍历整个工作区目录，断言 `secret-key` 不出现在任何文件里。

`redact` 的实现是「把内存中所有会话的 apiKey 拿出来做子串替换」（`service.py:224-230`），在 `run_task` 的异常路径统一使用（`service.py:437-438`）—— 因为 Provider 的报错文本可能回显 Authorization 头。⚠ 需要注意其边界：它只能脱敏**仍在内存中**的会话；重启后 `sessions` 为空，替换表也就空了（此时那些 Key 本身也已不存在）。

### 4.1 `_resolve_session`：为什么优先采用「本次请求带来的会话」

```python
def _resolve_session(task: dict, provided: str | None = None):
    if provided:
        sid = str(provided); session = store.get_session(sid)
        if not session:
            raise HTTPException(401, "LLM 会话已过期，请在「模型配置」重新保存后再重试")
        return session, sid
    stored = task.get("sessionId")
    if stored:
        return store.get_session(str(stored)), str(stored)
    return None, None
```
（`service.py:317-339`）

规则是：**请求带来的 `sessionId` 优先；未带来时回退到任务里记录的旧 ID。**

**为什么必须这样**：`store.sessions` 是内存态、TTL 1 小时且随重启失效，而 `task.json` 里存的是创建/上次运行时的旧 `sessionId`。用户重新配置后旧 ID 已死，端点若仍拿旧 ID 取会话 → 取不到 → 静默退化成空凭证 → 报「未配置 LLM 凭证」。用户照提示重新配置，仍然失败 —— 因为新会话**根本没被用上**（`service.py:320-327` 的注释把这条写成「实测最令人困惑的一点」）。回归测试 `tests/test_service_api.py:381-410` 直接复现该事故顺序。

注意两处的**不对称**，这是有意为之：

- `provided` 存在但取不到会话 → **立刻 401**，不静默降级（`service.py:333-334`）；
- 只有任务里的旧 ID（`provided` 为空）且已失效 → 返回 `(None, 旧ID)`，**不在这里报错**，改由 `run_task` 的凭证前置校验给出「会话已过期」的可操作提示（`service.py:348-358`）。这条路径就是「服务重启后用户直接点批准」的真实场景，测试见 `tests/test_service_api.py:915-973`。

### 4.2 变更端点都要接收并写回 `sessionId`

| 端点 | 读取 | 写回 |
|---|---|---|
| `POST .../approve` | `payload.pop("sessionId")`（先摘掉再存草稿） | `save_task(..., sessionId=sid)`（推进下一阶段时） |
| `POST .../resume` | `(body or {}).get("sessionId")` | `save_task(..., sessionId=sid)` |
| `POST .../modules/{module}/retry` | `(body or {}).get("sessionId")` | `save_task(..., sessionId=sid)` |
| `POST .../regenerate` | `(body or {}).get("sessionId")` | `save_task(..., sessionId=sid)` |

（`service.py:758,766`、`886-887`、`785-786`、`821,840-841`）

**为什么四个都要支持**：这些都是「运行失败或需要重来之后」的操作，此时距最初配置往往已过去很久（甚至服务重启过），旧会话几乎必然失效。若只在创建运行时可传会话，用户就会卡在一个无法自救的状态：重新配置 → 点恢复/重跑 → 仍报失败。写回任务则保证**后续阶段**继续用这个新会话，而不必每次都靠前端再带一次。

前端对应地把当前会话存进 `sessionStorage`，并在每个变更请求里带上（`ui/src/api.ts:32-38,52`）。它**不落 localStorage**，因此关闭标签页即失效 —— 与服务端 1 小时 TTL 的语义一致。

## 5. 凭证错误的分层提示

```python
if stub or dry_run or not requires_key or has_key: return None
if session_id and not session_alive:
    return "LLM 会话已过期（服务重启，或保存后超过 1 小时未操作）。请在「模型配置」重新保存会话，然后回到本页点「恢复」重试；已完成阶段的产物不会丢失。"
return "未配置 LLM 凭证：请先在「模型配置」保存会话，或在环境中设置对应 Provider 的 API Key。"
```
（`service.py:86-105`，字符串已按原意折行）

判据是 `session_id` 是否存在且 `session_alive` 是否为假：**「带了会话 ID 但取不到会话」= 过期**；**「压根没有会话 ID 也没有 Key」= 未配置**。

**为什么必须区分**：若把过期也写成「未配置 LLM 凭证」，用户会反复去「模型配置」重新保存，而真因是旧会话失效、新会话没被用上，于是**照提示重配也永远失败**。过期文案必须给出下一步动作（重新保存 → 回本页点「恢复」），并明确「已完成阶段的产物不会丢失」以降低焦虑。

**为什么抽成纯函数**（`service.py:94-96`）：stub 模式下 `run_task` 不会走到这段逻辑，若内联在 `run_task` 里，这两条消息**无法被任何端点测试触达**——变异测试因此漏报过一次（把「会话已过期」的提示删掉，所有测试仍然全绿）。单测见 `tests/test_service_api.py:843-873`，它同时断言两条消息不混用（`"过期" not in missing`）。

调用点在 `run_task` 的**最前面**（`service.py:348-358`），早于 `PipelineRunner` 构造 —— 测试用「打断桩 Runner 并断言它从未被构造」来观测这一顺序（`tests/test_service_api.py:945-973`）。

## 6. 单阶段重跑

### 6.1 语义

```python
_STAGE_DOWNSTREAM = {
    "analysis": ("analysis", "plan", "cases"),
    "plan":     ("plan", "cases"),
    "cases":    ("cases",),
}
```
（`service.py:794-801`）

`POST /api/v1/runs/{run_id}/stages/{stage}/regenerate`（`service.py:803-854`）做四件事：

1. **丢弃该阶段及其下游**的产物与检查点。用例产物尤其分散，需要逐个点名：`cases/cases`、`cases/.checkpoints`、`cases/.failures`、`cases/traceability.json`、`cases/用例集.md`、`cases/用例集.xlsx`、`cases/用例集.xmind`（`service.py:826-831`）。`_discard` 返回**实际删除的相对路径列表**，随响应回给前端做提示与审计（`service.py:862-875`）。
2. **重置阶段状态与模块状态**：从 `stageStates` 中剔除被丢弃的阶段，再把本阶段置回 `pending`；任务层写 `modules={}`、`errors=[]`（`service.py:834-841`）。
3. **重置 `run-state.json` 的 `modules`**：这一项决定阶段三是否会复用检查点。不复位的话，阶段三会认为「这些模块已经成功」而直接复用旧结果，重跑变成空转（`service.py:842-847`）。
4. 以 `resume=True` 重新入队（`service.py:851-853`）：复用**上游**产物，本阶段产物已删除故必然重新生成。

响应是任务对象加上 `discarded` 字段：`{**store.task(run_id), "discarded": discarded}`（`service.py:854`）。产物根在删除前先过 `_safe_output` 校验（必须位于工作区内，否则 400），与产物清单/下载接口的穿越防护同源（`service.py:856-860`）。

### 6.2 为什么下游必须一并丢弃

下游产物是**以上游产物为输入**生成的：测试方案引用需求分析里的 REQ 编号，用例引用方案里的范围与优先级口径，追踪矩阵把用例映射回 REQ。若只丢上游、留下游，就会出现「新分析报告 + 旧方案」的组合：追踪矩阵里的 REQ 可能已不存在、模块清单与方案口径不一致，而 UI 上还显示这些阶段「已确认」。这类不自洽不会立刻报错，只会在评审或导出时才暴露，代价远高于重跑。

代码里连**状态**也一并清掉，正是为了避免「UI 显示已确认、产物却已被丢弃」的错觉（`service.py:834-836` 的注释）。

### 6.3 拒绝条件

| 条件 | 状态码 | 消息 |
|---|---|---|
| 运行不存在 | 404 | 运行不存在 |
| `stage` 不在 `{analysis,plan,cases}` | 400 | stage 必须为 analysis、plan 或 cases |
| `status ∈ {pending, running}` | 409 | 运行进行中，请先取消再重跑该阶段 |
| 任务没有 `output` | 409 | 该运行尚无产物，无法重跑阶段 |

（`service.py:815-820`，测试 `tests/test_service_api.py:584-595`）

**为什么「运行中」要拒绝**：重跑会删除产物目录，而与运行中的 pipeline 正在往同一目录写文件 —— 结果既不可预期，也不可审计。**为什么「无产物」要拒绝**：没有产物就没有可判定的上游，此时正确操作是直接新建运行。

前端把这两个条件也做了镜像：只有 `['approved','succeeded','failed','awaiting_review','rejected']` 相位、且整体不在 `pending/running` 时才显示「重跑」，并用 `Popconfirm` 说明影响下游（`ui/src/components/StageSteps.tsx:125,137-140`）。

## 7. 前端工作台

### 7.1 页面与路由

`ui/src/App.tsx:1-4` 用 antd `Layout` + `react-router-dom` 组装，四个页面全部 `lazy` + `Suspense`：

| 路由 | 页面 | 职责 |
|---|---|---|
| `/` | `pages/Dashboard.tsx` | 运行列表；新建运行（逐阶段审阅）；一键运行 |
| `/prd` | `pages/PrdPage.tsx` | 上传（文件/多文件/文件夹）、原文编辑 + Markdown 预览、保存新版本、重新分析 |
| `/runs/:id` | `pages/RunPage.tsx` | 运行详情：三阶段卡片、批量选择、实时指标、模块状态、质量门禁、Token、事件流、导出 |
| `/settings` | `pages/Settings.tsx` | LLM Provider 会话配置（检测并保存 / 仅检测） |
| `*` | 回落到 `Dashboard` | |

侧栏菜单选中键直接用 `location.pathname`，因此 `/runs/:id` 时无高亮项（`App.tsx:1`）。`PrdPage` 支持 `?id=<prdId>` 深链（`PrdPage.tsx:21-22`），便于从别处跳转到指定版本。

两个仪表盘动作的差异值得注意：

- **一键运行**：取**列表第一条 PRD**（最新）与 `sessionStorage` 中的会话，`auto:true`、`jobs:4`、跳过逐阶段审阅；审阅记录会被标成 `one-click`/`auto`（`Dashboard.tsx:21-31`、`service.py:424-427`）。
- **新建运行（逐阶段审阅）**：可选 PRD/Profile/模块并发数，`stub` 默认关闭 —— 正式工作流默认真实模型，离线 Stub 只能显式开启，避免开发期的固定样例悄悄成为生产产物（`Dashboard.tsx:12-14,55`）。没保存会话且未开 stub 时直接拦下并提示（`Dashboard.tsx:19`）。

### 7.2 三阶段独立卡片（`StageSteps`）

`StageSteps` 把运行拆成三张卡：① 需求分析 / ② 测试方案 / ③ 用例设计，各自显示相位、耗时、模型、token 与**只属于该阶段**的操作（`ui/src/components/StageSteps.tsx:12-16,118-145`）。

**为什么放弃「一个全局 0–100% 进度条」**：后端的 `percent` 只有 33/66/100 三档（`service.py:428`），它表达的是「走到第几步」，而不是「这一步做得怎么样」。用户真正的问题是「现在这一步在做什么、还差多少、失败在哪一段」，单一进度条一个都答不了。拆成三张卡后，每张卡可以各自回答，且各阶段的操作互不干扰。

相位判定必须**同时看 `stageStates` 与 `currentStage`**：

```ts
const state=(run.stageStates?.[stage]||'') as StagePhase;
if(state)return state;
if(run.currentStage===stage&&['pending','running'].includes(run.status))return 'running';
return 'pending';
```
（`StageSteps.tsx:34-39`）

**为什么**：`stageStates` 是后端的权威记录，但「**正在跑**」这件事只在 `currentStage` 上 —— `run_task` 先把 `currentStage` 置为当前阶段并置 `status=running`，此时 `stageStates[current]` 已是 `"running"`；而重新入队的瞬间（如批准后进入下一阶段）`stageStates[next]` 被写成 `pending`、`currentStage` 已是 `next`，若只看 `stageStates` 就会把正在运行的阶段显示成「待开始」。前端专门用例：`ui/src/test/StageSteps.test.tsx:51-57`。

阶段三显示**真实模块进度**，其余阶段显示**不确定态**：

```ts
const progress=info.key==='cases'&&phase==='running'?casesProgress(run):undefined;
...
{phase==='running'&&(progress
  ? <Progress percent={progress.percent} size="small" status="active"
      format={()=>`${progress.done}/${progress.total} 模块`}/>
  : <Progress percent={100} size="small" status="active" showInfo={false}/>)}
```
（`StageSteps.tsx:122,147-151`）

- 阶段三的刻度是**真的**：已落定模块（`succeeded`/`skipped`）/ 总模块（`StageSteps.tsx:57-66`），显示成 `2/4 模块`。
- 阶段一/二没有可用的细分刻度，于是显示 `status="active"` 的**不确定态**进度条 + 已用时间，**不编造百分比**（`StageSteps.tsx:58-60` 的注释明确写了这条取舍）。
- 数据来源是运行详情里的 `modules`（`service.py:404-405`），不是前端估算。

每张卡还显示该阶段自己的元数据：耗时（`durationMs`）、模型（`model`）、token（`tokens`），来自 `GET /runs/{id}` 注入的 `stageMeta`（`StageSteps.tsx:133-135`、`service.py:699-707`）；测试断言见 `ui/src/test/StageSteps.test.tsx:118-123`。

失败态则给出错误详情 + 门禁错误条数 + 「重跑」出口（`StageSteps.tsx:152-157`）。

### 7.3 去掉了「先保存草稿再批准」

用户的动作只剩一个：**「确认并进入下一步」**（阶段三文案为「完成并结束」）。按钮只在「相位=待确认且是当前阶段」时出现（`StageSteps.tsx:124,141-144`）。

服务端 `approve` 本就接受可选 body，等价于「先存草稿再批准」（`service.py:750-759`）；批准时会 `apply_draft` 把**最新已保存草稿**应用到产物目录（`service.py:760`）。因此正常路径不需要用户关心草稿这件事。

⚠ 需要准确理解的一点：工作台的 `confirm` 只发 `{sessionId}`，**不带内容**（`ui/src/api.ts:52`、`StageSteps.tsx:96-102`，测试断言 `approve` 以 `('run-1','analysis')` 被调用，`StageSteps.test.tsx:82-87`）。所以「未点保存修改」的编辑**不会**随确认提交；它们由 `StageEditor` 上报的「有未保存修改」金色标签提示（`StageSteps.tsx:131`、`ui/src/components/StageEditor.tsx:17-19`）。要携带修改，先点面板里的「保存修改」。

编辑始终是**可选**的：`StageEditor` 刻意不做自动保存（`StageEditor.tsx:9`），「保存修改」按钮在无改动时禁用（`StageEditor.test.tsx:23-24`）。

### 7.4 两处必须修的前端缺陷

**（1）「查看产物」必须让 `Collapse` 受控**

```tsx
const [expanded,setExpanded]=useState<Stage[]>([]);        // 用数组：允许多阶段同时展开
<Collapse activeKey={isOpen?['artifact']:[]} onChange={keys=>setExpanded(...)} />
```
（`StageSteps.tsx:78,160-167`）

未受控时，「查看产物」按钮**无法从外部展开面板**：按钮只能去改 `RunPage` 的 `stage` 状态，而没有任何渲染消费它，于是点击后界面毫无反应（`StageSteps.tsx:73-77` 的注释称之为「实测的真实缺陷」，回归测试 `ui/src/test/StageStepsArtifact.test.tsx:48-56`）。用**数组**而非单值，是为了允许多个阶段同时展开以便对比（同文件 `:74-84` 断言两卡可同时展开）。`onChange` 仍把用户手点面板头的动作同步回同一状态，所以手动收起依然有效（`StageStepsArtifact.test.tsx:65-72`）。

**（2）`onDirtyChange` 必须放进 ref**

```tsx
const dirtyCallbackRef=useRef(onDirtyChange); dirtyCallbackRef.current=onDirtyChange;
useEffect(()=>{dirtyCallbackRef.current?.(dirty)},[dirty]);   // 只依赖 dirty
```
（`StageEditor.tsx:11-19`）

父级传的是内联箭头函数，每次渲染都是新引用。若把它直接写进依赖数组，就会形成「effect 触发 → setState → 重渲染 → 新回调 → effect 再触发」的**无限循环**（注释记录：面板一展开页面即卡死）。这个缺陷此前被隐藏，正是因为面板根本无法展开 —— 修好缺陷（1）之后它才暴露出来。这也解释了为什么两者总是成对出现。

### 7.5 批量选择（`CaseFilterPanel`）

三维度（模块 / 优先级 / 标签）多选 + 关键词，每维度把候选值与**实时数量**渲染成 `Tag.CheckableTag`（`ui/src/components/CaseFilterPanel.tsx:19-23,85-104`）。

- 数量来自服务端 `facets`，且**统计该维度时忽略它自身的约束**，因此用户能在选择过程中比较「换成 P1 会剩多少条」，而不是盲选。
- 前端一次请求取全：`fetchFacets(runId, selection, '*')`（`CaseFilterPanel.tsx:42`）。逐维度请求再合并会让三个响应互相覆盖，计数随机取决于哪次最后到达（同文件 `:39-41` 的注释；测试断言只调用一次：`ui/src/test/CaseFilterPanel.test.tsx:38-45`）。
- 顶部显示「已选 N/M 条（排除 K）」；无筛选时显示「共 M 条用例」（`CaseFilterPanel.tsx:71-81`，测试 `CaseFilterPanel.test.tsx:66-77`）。
- 并发保护：用递增序号 `seqRef` 只接受**最后一次**响应，避免旧结果覆盖新结果（`CaseFilterPanel.tsx:32-49`）。
- 「禁用只阻止交互，不隐藏信息」：`disabled` 时仍完整加载并展示可选项，否则用户看不出「是没得选」还是「被禁用了」（`CaseFilterPanel.tsx:51-54`，测试 `:97-105`）。
- 关键词在 `onBlur`/回车时才提交给父级（`CaseFilterPanel.tsx:107-111`），避免每敲一个字符就打一次分面请求。
- 批量选择面板放在导出按钮**之后**、实时指标**之前**（`RunPage.tsx:42-45`）：先决定「要哪些」，再决定「怎么导出」。

### 7.6 导出对话框（`ExportDialog`）

可选：格式（Excel/XMind）、文件名、XMind 粒度（到用例/到步骤/仅模块）、Sheet 名（限 31 字符）、保存位置（`ui/src/components/ExportDialog.tsx:84-113`）。

流程是「拉取 Blob → 另存为」：

- `fetchExport` 用 `responseType:'blob'` 取回文件（`api.ts:117-124`）；
- `saveBlob` 优先调 `showSaveFilePicker` 让用户选位置与文件名，不支持时回退为 `<a download>` 指定文件名的下载（`api.ts:127-159`）；
- 默认文件名由 `defaultExportName` 生成，含 product/version/「用例集」/筛选片段/日期，例如 `PRD-v0.1-用例集-KB-P0-20261013.xlsx`（`api.ts:106-112`，测试 `ExportDialog.test.tsx:26-37`）；
- 每次打开按当前格式与筛选**重置**默认名，避免上次的输入串到这次（`ExportDialog.tsx:44-45`）；
- 用户只写主名时自动补扩展名（`ExportDialog.tsx:51-54`）。

三个细节：

1. **用户取消不算失败**。`showSaveFilePicker` 被取消时抛 `AbortError`，`saveBlob` 把它映射为 `'cancelled'`，对话框提示「已取消保存」且**不关闭**（`api.ts:148-151`、`ExportDialog.tsx:64`，测试 `ExportDialog.test.tsx:118-125,193-197`）。
2. **`responseType:'blob'` 会把错误响应也包成 Blob**，必须读回文本才能拿到后端 `detail`，否则用户只看到笼统的「导出失败」（`ExportDialog.tsx:9-26,68-76`；`Blob.text()` 在旧浏览器/jsdom 不可用，回退 `FileReader`）。
3. **不再用 `window.open`**：那样无法自定义文件名、无法选保存位置，也**无法感知失败**（浏览器会把错误页静默存成文件，`ExportDialog.tsx:28-33`）。

### 7.7 查询参数序列化（重要前端坑）

axios 默认把数组序列化成 `modules[]=KB&modules[]=AGENT`，而后端 `List[str] = Query(None)` 只认**重复键** `modules=KB&modules=AGENT`。实测后果：后端**完全收不到**筛选参数 → 视为无筛选 → 用户明明选了条件，导出却仍是全量，而且**返回 200**（成功响应掩盖了筛选丢失）。因为这是「静默错误」而非报错，很难在联调时发现。

修复是自定义 `paramsSerializer`（`ui/src/api.ts:4-31`）：

```ts
const serializeParams=(params)=>{ const search=new URLSearchParams();
  for(const [key,value] of Object.entries(params||{})){ /* 空值/空数组跳过；数组用 search.append 重复追加同键 */ }
  return search.toString(); };
export const api=axios.create({baseURL:'/api/v1',timeout:120000,paramsSerializer:{serialize:serializeParams}});
```
（`ui/src/api.ts:14-31`，展开见原文）

顺带统一两条约定：

- **空数组不发参数**：空集合的语义是「什么都不要」，而业务要的是「该维度不限」。`selectionParams` 只在 `length` 非 0 时才写入（`api.ts:58-70`）。
- `undefined`/`null`/空串同样不发，避免后端把 `""` 当成有效取值。

回归测试 `ui/src/test/StageStepsArtifact.test.tsx:87-123` 直接检查 `api.getUri()` 的产物：不得出现 `[]`，必须出现 `modules=KB` 与 `modules=AGENT`，空数组不得产生参数。

### 7.8 `StageEditor` 的两处数据正确性设计

**（1）行的定位必须用稳定 `id`**

```ts
const keyOf=(row:any,index:number)=>String(row?.id ?? row?.caseId ?? `#${index}`);
```
（`StageEditor.tsx:20-31`）

antd Table 的 `render(value, record, index)` 里第三个参数是**当前页内序号**，不是全局下标。用它做选择会（注释记录了实测后果）：第 2 页点「详情」打开的是第 1 页同位置的用例；第 2 页改标题/状态会**静默改到第 1 页那一条** —— 后者是数据损坏，比显示错误严重得多，且只有保存后才会发现。因此行定位一律用 `id`（`rowKey` 已是 id，唯一），仅当 id 缺失（契约上不该发生）才退化到下标。回归测试 `ui/src/test/StageEditorPaging.test.tsx:48-117` 用 15 条用例（必然两页）逐项验证详情、改标题、切状态、抽屉改名都只影响目标行。

**（2）后台轮询必须 `keepEdits`**

```ts
const load=async({keepEdits=false}={})=>{ ... if(!keepEdits){ /* 覆盖 markdown/rows */ } ... };
useEffect(()=>{ ...; load(); const timer=window.setInterval(()=>load({keepEdits:true}),5000); return()=>window.clearInterval(timer); },[runId,stage]);
```
（`StageEditor.tsx:32-56`）

原先每 5 秒无条件重载，会把用户刚打的字**静默冲掉**，再点「确认」时保存的是服务端旧值而非他的修改（注释称之为「实测的真实数据丢失路径」）。现在轮询路径不覆盖 `markdown`/`rows`，但**产物清单与追踪矩阵仍照常刷新**，因此外部变化依然可见。回归测试 `ui/src/test/StageEditor.test.tsx:41-56` 连续触发两轮轮询后断言编辑内容仍在。

该面板按阶段提供不同页签：`analysis` → 结构化需求 / Markdown 源码与预览 / JSON；`plan` → 方案源码与预览；`cases` → 结构化用例 / JSON / 追踪矩阵（`StageEditor.tsx:69`）。追踪矩阵页签给出覆盖/不足/未覆盖/覆盖率与未知引用、重复映射告警（`StageEditor.tsx:68`）。

### 7.9 实时指标面板（`RunProgress`）

`RunProgress` 消费 WS 推来的 `run_metrics` 快照（经 `useRun` 的 `metrics` 状态，`hooks.ts:29`）。头部一行：当前阶段、耗时、总 Token、缓存读取、活跃模块数、静默时长，以及「已取消」「已结束」标记（`ui/src/components/RunProgress.tsx:13`）。表格按模块一行（`RunProgress.tsx:11-12`）：

| 列 | 含义与判读方式 |
|---|---|
| 模块 | 悬停显示需求条数、请求次数、流式增量块数 |
| 状态 | `pending/running/done/failed/skipped`；运行中且暂无正文但有思考 → 紫色「思考中」；有正文 → 青色「产出中」；`reasoningOnly` → 「仅思考」（输出预算可能被思考吃光） |
| 批次 | `batchIndex/batchTotal`，看当前模块切到了第几批 |
| Token（提示/生成） | 估算值前标 `≈` 并注明「字符估算值，非 Provider 上报」 |
| tok/s | 生成速率 |
| 首 Token | `firstTokenMs`；未观测到时为 `-`（0 无法区分「瞬时响应」与「还没开始产出」，`metrics.py:235-236`） |
| 静默 | 距上次收到内容的时长；`>20s` 橙色、`>60s` 红色 —— **用于判断是否卡住** |
| 思考 | 推理字符数 ≈ token，与产出 token 分列，避免把「正在思考」误报成「正在产出」 |
| 最新输出（尾部） | 最近 600 字符预览（`metrics.py:27-28`），肉眼确认模型确实在产出 |

**为什么要区分「静默」与「耗时」**：长任务的关键问题不是「跑了多久」而是「最后一条内容是什么时候来的」。静默时长直接暴露卡住；配合服务端每 5s 一条心跳事件（`service.py:385-397`），即使没有 token 增量，界面也不会看起来像死掉。

`≈` 语义由后端保证：字符估算值一律带 `estimated:true`，拿到 Provider 精确 `usage` 后校准并翻转标记（`metrics.py:128-151,209-212`）。

## 8. 并发与竞态

### 8.1 `_json` 读取必须重试

```python
for attempt in range(6):
    try:
        with self._lock:
            if not path.exists(): return fallback
            return json.loads(path.read_text(encoding="utf-8"))
    except PermissionError as exc:            # 文件正被替换：稍后重读
        last_error = exc; time.sleep(0.02 * (attempt + 1))
    except (OSError, json.JSONDecodeError):
        return fallback                       # 被删/损坏不是瞬时问题，直接按默认值处理
return fallback
```
（`service.py:168-196`，注释为原文）

**为什么读方也会失败**：`io.atomic_write_text` 用 `os.replace` 原子替换，而 Windows 在目标文件**正被打开或正被替换**时会让**读方**也拿到 `PermissionError`（共享冲突）。写方的重试救不了读方 —— 实测 `GET /runs/{id}` 在运行中偶发 500，栈帧落在本函数的 `read_text`（`service.py:169-178`，测试 `tests/test_service_api.py:769-801` 用「前两次读抛 `PermissionError`、第三次成功」精确复现该瞬时状态）。重试总时长约 0.42s（0.02×(1+…+6)），与 `atomic_write_text` 的重试量级一致。

**锁为什么不够**：产物文件（`run-manifest.json`、`run-state.json` 等）由 pipeline **直接写入、不经过本 store 的锁**，所以 `self._lock` 无法消除这条竞态；锁只保护同一 store 实例内部的一致性。

**为什么重试仍失败时返回默认值而不是抛错**（`service.py:194-196`）：读不到某个**辅助产物**不应让整个请求 500。而 `GET /runs/{run_id}` 上少一块信息只是前端少显示一块，比整页失败好得多。默认值契约是：`default` 未给出（`None`）时回退为 `{}`，调用方靠 falsy 判断「不存在」（如 `store.task()` 返回 `{}` 或 `None`）；测试 `tests/test_service_api.py:804-829`。

注意异常分类很关键：`OSError`（含文件被删）与 `JSONDecodeError`（内容损坏）**不重试**，直接返回默认值 —— 它们不是瞬时问题。只有 `PermissionError` 才是「正在被替换」这个合法瞬时状态。

### 8.2 其他并发点

| 位置 | 机制 | 目的 |
|---|---|---|
| `WorkspaceStore._lock` | `threading.RLock`，覆盖 `_json`/`save_task`/`event`/`event_list` | `save_task` 内部会再调 `_json`，必须可重入 |
| `LiveMetrics._lock` | `threading.RLock`，保护全部可变状态 | `jobs>1` 时多模块并发写指标（`metrics.py:10`） |
| 模块归因 | `threading.local()` 存「当前模块」 | 线程池里每个 worker 负责一个模块，把流式回调归因到正确模块，避免把 `on_delta` 穿过 6 层函数签名（`metrics.py:283-303`） |
| 取消标志 | `CancelFlag`（`threading.Event`）+ `bind_cancel` | 流式回调逐块检查，命中即停止接收响应（`metrics.py:306-326`） |
| 前端分面请求 | `seqRef` 序号 | 只接受最后一次响应（`CaseFilterPanel.tsx:32-49`） |
| 前端 WS 重连 | 指数退避 `min(1000*2**attempts, 10000)` | 终态后不再重连（`hooks.ts:33`） |

## 9. 开发与构建

| 命令 | 作用 |
|---|---|
| `npm start` | `= npm run dev:all`，同时起 FastAPI(8000) + Vite(5173) |
| `npm run dev` / `npm run dev:backend` | 只起 Vite / 只起 uvicorn |
| `npm run build` | `tsc -b && vite build` → `ui/dist` |
| `npm test` | `vitest run`（`ui/package.json`） |

前端测试用 **vitest + jsdom**（`ui/vitest.config.ts`），共 **8 个测试文件**：

`ui/src/test/` 下 `StageSteps.test.tsx`、`StageStepsArtifact.test.tsx`、`StageEditor.test.tsx`、`StageEditorPaging.test.tsx`、`CaseFilterPanel.test.tsx`、`ExportDialog.test.tsx`、`RunProgress.test.tsx`、`PrdPage.test.tsx`（另有非测试的 `setup.ts`）。

`vitest.config.ts` 把 `testTimeout`/`hookTimeout` 放宽到 **20s**，原因是：antd 组件在 jsdom 下首次渲染常需 1~3s（含 rc-* 动画与 ResizeObserver 兜底），默认 5s 会让含多次 `findBy*` 的用例随机超时 —— 单独跑通过、与其他文件并发时失败（`ui/vitest.config.ts` 注释）。`restoreMocks:true` 保证每个用例后恢复 mock。

后端测试中与服务模式直接相关的文件：`tests/test_service_api.py`（48 个用例，覆盖上传/版本/会话/凭证/重跑/分面/导出/竞态/WS/SPA）、`tests/test_realtime.py`（实时事件、心跳、指标推送、取消止损、WS 兼容性护栏）、`tests/test_casefilter.py`（筛选与分面语义）、`tests/test_export.py`。

**生产部署**：

1. `cd ui && npm run build`；
2. `create_app(ui_dir="ui/dist")` 或 `uvicorn 'prd2tc.service:create_app' --factory`（默认会找仓库内 `ui/dist`）；
3. `--host 0.0.0.0` 对外时务必清楚：接口**无鉴权**，且 LLM 会话以 `sessionId`（`secrets.token_urlsafe(24)`）作为唯一凭据 —— 拿到 `sessionId` 就等于可以用你的 Key 发请求。

## 10. 未验证与已知边界

以下内容在本文写作时**未通过运行验证**，只依据代码/注释/测试所表述的事实：

- > ⚠ 未验证：缺少 `websockets` 时「实时推送静默退化为 10 秒轮询」的实际表现。该结论来自 `README.md:79-80` 与 `pyproject.toml` 注释；代码层面的间接证据是 `hooks.ts:36` 确实挂了 10s 轮询兜底。
- > ⚠ 未验证：`Dashboard.tsx:57` 中「串行 5 模块约 1013s、并发 5 约 332s」与 `StageSteps`/`StageEditor` 注释里提到的若干实测数字，本文只做转引，未重新测量。
- > ⚠ 未验证：`ui/dist` 的当前构建是否与 `ui/src` 同步。目录存在且含 `index.html`（`test_production_spa_fallback` 会因此真正执行而非跳过），但未比对构建时间与源码时间。
- > ⚠ 未验证：`_download_name` 对**非 ASCII 文件名**在 `Content-Disposition` 中的编码行为由 Starlette/`FileResponse` 决定；服务端测试只断言响应头里出现原始中文或百分号编码两种形式之一（`tests/test_service_api.py:765-766`）。
- **WebSocket 无鉴权、无 per-run 订阅权限校验**：任何知道 `runId` 的客户端都能订阅该运行的事件流。
- **`GET /runs/{run_id}` 只返回末 100 条事件**（`service.py:708`），完整历史需要调 `GET /runs/{run_id}/events`。
- **事件文件按 `sent` 下标增量推送**（`service.py:985-990`），依赖文件只追加。若人工截断或重写 JSONL，已连接的客户端下标会错位。
- **导出会写入运行产物目录**（`export_cases(..., root=output)`），多次导出会在 `outputs/<taskId>/` 累积文件。
