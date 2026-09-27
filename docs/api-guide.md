# 后端 API 手册

> 服务地址：`http://127.0.0.1:8000`　｜　交互式文档：`/docs`（Swagger）、`/redoc`
> 代码位置：`backend/app/api/`　｜　响应模型：`backend/app/schemas/`

## 1. 通用约定

### 1.1 响应模型与 ORM 分离

前端看到的是 `app/schemas/` 里的 Pydantic 模型，**不是** ORM 模型。
这样数据库字段改名不会无声地破坏前端契约。

### 1.2 错误格式

FastAPI 统一返回：

```json
{"detail": "合同不存在: 123"}
```

| 状态码 | 含义 | 典型场景 |
|---|---|---|
| 400 | 请求非法 | 文件为空、格式不支持、业务类型非法 |
| 404 | 资源不存在 | 合同 / 任务 / 风险项 / 导出记录不存在 |
| 409 | 状态冲突 | 非 blocked 任务调重试；DOCX 尚无渲染用 PDF |
| 413 | 文件过大 | 上传超过 20MB |
| 422 | 参数校验失败 | 缺少必填查询参数（FastAPI 自动） |
| 502 | 下游不可用 | MinIO 读取失败、审批系统不可达 |

### 1.3 幂等

- `POST /api/contracts/upload`：`file_hash` 命中未删除合同时**直接返回已有记录**，不重复解析
- `POST /api/tasks/sync-todos`：按 `file_hash` 去重，已存在的合同不重复建任务
- `POST /api/contracts/{id}/writeback`：内容 hash 相同则复用回写记录（`deduplicated: true`）

### 1.4 软删除

只有 `contract` 支持软删除（原则 P6）。删除后：
- 列表与详情均返回 404
- 数据库行仍在，`deleted_at` 非空
- 同一 `file_hash` 可重新上传（唯一键建在生成列 `deleted_key` 上）

## 2. 接口清单

### 2.1 元信息

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/meta/config` | 前端可见的运行时配置（**不含任何凭据**） |

### 2.2 合同 `contracts`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/contracts` | 合同列表（大盘页）；支持 `status` / `risk_level` 过滤与分页 |
| GET | `/api/contracts/{id}` | 合同详情（含 `task_id`、`pdf_object_key`） |
| POST | `/api/contracts/upload` | 上传合同（multipart）；`auto_review=true` 时立即启动审查 |
| DELETE | `/api/contracts/{id}` | 软删除 |
| GET | `/api/contracts/{id}/clauses` | 条款列表（工作台左栏） |
| GET | `/api/contracts/{id}/metadata` | 提取的元数据 |
| GET | `/api/contracts/{id}/events` | 任务事件轨迹（审计） |
| GET | `/api/contracts/{id}/parse-results` | 解析历史（重试后 `attempt` 递增） |
| GET | `/api/contracts/{id}/pdf` | **渲染用 PDF**（DOCX 返回转换产物，PDF 返回原件） |
| GET | `/api/contracts/{id}/file` | 合同原件下载 |

**`/pdf` 为什么必需**：前端统一用 PDF.js 渲染（架构 §14.3），
而 `pdf_object_key` 只是 MinIO 的 key，浏览器无法直接访问。
该接口由后端代理对象存储，前端不直连 MinIO（省掉跨域与凭据配置）。

| 情形 | 行为 |
|---|---|
| DOCX | 返回 `pdf_object_key` 指向的转换产物；尚未生成时 **409** |
| PDF | 直接返回原件 |
| 图片 | 阶段一未接入 OCR，返回 **409** |

### 2.3 任务 `tasks`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/progress` | 进度（前端轮询；优先读 Redis，回退数据库） |
| POST | `/api/tasks/{id}/retry` | 重试阻塞任务（仅 `blocked` 可重试，否则 409） |
| POST | `/api/tasks/sync-todos` | 从审批系统拉待办建合同 + 任务 |

### 2.4 风险 `risks`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/risks?contract_id=` | 风险清单（含锚点与依据链，一次查完避免 N+1） |
| GET | `/api/risks/{id}` | 风险详情 |
| PATCH | `/api/risks/{id}` | 法务编辑建议 / 标记采纳 |
| POST | `/api/risks/annotations` | 新增法务批注 |
| GET | `/api/risks/annotations/list?contract_id=` | 批注列表 |

**不变量 I4**：`suggestion_edited` 非空 ⟹ `adopted = 1`。
后端在 PATCH 里显式维护，调用方不必传对：

```jsonc
// 只传编辑版建议，后端自动补 adopted=true
{"suggestion_edited": "建议改为：验收合格后 15 日内付款。"}

// 空串表示清空（回退到 AI 原始建议）
{"suggestion_edited": ""}
```

### 2.5 规则库 `rules`（阶段一只读）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/rules/templates` | 规则模板列表 |
| GET | `/api/rules/rules` | 规则列表 |
| GET | `/api/rules/rules/{id}` | 规则详情 |
| GET | `/api/rules/standard-clauses` | 示范条款库 |
| GET | `/api/rules/blacklist` | 主体黑名单（⚠️ mock 数据，非真实工商信息） |

### 2.6 报告 `reports`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/contracts/{id}/report/preview` | 报告 Markdown 预览（不落存储） |
| POST | `/api/contracts/{id}/report/export` | 导出到 MinIO，登记 `export_record` |
| GET | `/api/contracts/{id}/report/download/{rid}` | 下载报告（后端代理 MinIO） |
| GET | `/api/contracts/{id}/report/exports` | 导出历史 |

### 2.7 回写 `writeback`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/contracts/{id}/writeback` | 写回审批系统评论区 |
| GET | `/api/contracts/{id}/writeback/status` | 回写状态（前端轮询） |
| GET | `/api/contracts/{id}/writeback/logs` | 回写日志 |

回写状态机（`docs/architecture.md` §7.2）：

```
not_written ──> writing ──> success
                    └─────> failed（可重试）
```

**实现要点**：先落 `writing` 并 commit，再调审批系统，最后更新结果。
这样进程崩溃时能看出"曾发起过回写"，而不是状态丢失。

失败分两类：

| 异常 | 含义 | 可否重试 |
|---|---|---|
| `WritebackBlocked` | 前置条件不满足（如任务未完成） | ❌ 不可重试 |
| `ApprovalError` | 审批系统调用失败 | ✅ 可重试 |

### 2.8 事件 `events`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/events/approval` | 接收 mock 审批系统的事件推送 |

## 3. 关键数据契约

### 3.1 锚点（`AnchorOut`）——前端高亮的依据

```json
{
  "page_no": 1,
  "bbox_x0": 90.0, "bbox_y0": 470.5,
  "bbox_x1": 376.1, "bbox_y1": 508.0,
  "char_start": 251, "char_end": 285,
  "quote_text": "第五条违约责任\n乙方逾期交货的...",
  "anchor_level": "exact",
  "source": "native_text"
}
```

| 字段 | 说明 |
|---|---|
| `bbox_*` | **PDF point 坐标，左上原点**（非像素）。前端乘 scale 即得像素 |
| `char_start/end` | 全文字符区间；**段落级锚点为 `null`** |
| `anchor_level` | `exact` / `fuzzy` / `paragraph`；`none` 不写库 |

**⚠️ 坐标约定是前后端最容易错的地方**：不要用 PDF.js 的
`viewport.convertToViewportRectangle()`，它假定输入是 PDF 原始坐标系
（左下原点）会翻转 y。正确做法见
`frontend/src/lib/anchorCoords.ts`（唯一实现，前端组件与回归校验共用）。

`anchor_level` 的取值由规则类型决定：

| 规则类型 | 级别 | 原因 |
|---|---|---|
| KEYWORD / REGEX / BLACKLIST | `exact` 或 `fuzzy` | 能提取命中子串，覆盖整段引用 |
| PRESENCE | `paragraph` | "必须包含某模式"没有命中子串，只定位到条款标题块 |

### 3.2 风险项（`RiskItemOut`）

| 字段 | 说明 |
|---|---|
| `merged_by` | `rule` / `llm` / `both`，双来源留痕 |
| `unanchored` | 引用无法定位（防幻觉闸门标记）；此类项 `anchors` 为空 |
| `evidences` | 依据链，`evidence_type` 为 `rule` 或 `llm` |
| `need_review` | 法条未经知识库校验，提示人工复核 |
| `suggestion_edited` | 法务编辑版；非空时报告优先展示它 |

### 3.3 合同列表项（`ContractListItem`）

列表页字段由后端**一次聚合**（冗余 `review_task` 的状态与计数），
前端不做 N+1 查询。金额是 `Decimal` 序列化后的字符串，如 `"2299000.00"`。

## 4. 前端集成要点

### 4.1 开发期代理

`frontend/vite.config.ts` 把 `/api` 与 `/health` 代理到 `127.0.0.1:8000`，
前端代码统一用相对路径，无需处理 CORS 与 base_url。

后端 CORS 放行 `http://localhost:*` 与 `http://127.0.0.1:*`
（阶段一为本地开发环境）。

### 4.2 审查进度的轮询

审查在后台线程执行，前端靠 `GET /api/tasks/{id}/progress` 轮询：

- `live_progress` 形如 `"2/2"`，来自 Redis（解析阶段高频写入）
- Redis 不可用或键过期时回退到数据库的 `parsed_pages`
- 状态变为 `completed` / `blocked` 后停止轮询

### 4.3 双向锚定的前端实现

- 风险卡片 → 正文：用 `page_no` 跳页，用 `bbox` 滚动 + 高亮
- 正文 → 风险卡片：点高亮框（或文本层 span）→ 按 `risk_item_id` 定位卡片

## 5. 端到端链路（实测）

```
POST /api/tasks/sync-todos        → 建合同 + 任务（从 mock 拉附件）
     Pipeline.run()               → 6 项风险（4 高 / 2 中）
GET  /api/contracts/{id}          → completed / high / reject / 金额 2299000.00
GET  /api/risks?contract_id=      → 6 项，锚点与依据齐全
PATCH /api/risks/{id}             → adopted=true（维护 I4）
POST /api/risks/annotations       → 批注创建
GET  /report/preview              → 含法务编辑建议与"已采纳"标记
POST /report/export               → 落 MinIO 并登记 export_record
GET  /report/download/{rid}       → 下载报告
POST /writeback                   → success，返回 comment_id
GET  /writeback/status            → success
POST /writeback（再次）           → deduplicated=true，复用同一日志
GET  /events                      → 状态流转审计轨迹
```

回归断言集执行器：`python scripts/run_tests.py --verify-anchors`
（见 `docs/architecture.md` §16.2）。
