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
| 409 | 状态冲突 | 非 blocked 任务调重试；DOCX 尚无渲染用 PDF；规则编码重复；删除被历史依据引用的规则 |
| 413 | 文件过大 | 上传超过 20MB |
| 422 | 参数校验失败 | 缺少必填查询参数、`ids` 为空或超过 200 条（FastAPI 自动） |
| 502 | 下游不可用 | MinIO 读取失败、审批系统不可达 |

### 1.3 幂等

- `POST /api/contracts/upload`：`file_hash` 命中未删除合同时**直接返回已有记录**，不重复解析
- `POST /api/tasks/sync-todos`：按 `file_hash` 去重，已存在的合同不重复建任务
- `POST /api/contracts/{id}/writeback`：内容 hash 相同则复用回写记录（`deduplicated: true`）

### 1.4 列表筛选参数

| 参数 | 取值 | 说明 |
|---|---|---|
| `status` | `pending` / `parsing` / `reviewing` / `completed` / `blocked` / `failed` | 按任务状态 |
| `risk_level` | `high` / `medium` / `low` | 按综合风险等级 |
| `business_type` | `purchase` / `sales` / `service` / `labor` | 按业务类型 |
| `keyword` | 任意字符串 | 合同名称 / 编号模糊匹配 |

> ⚠️ **风险等级的参数名是 `risk_level`**。曾误写作 `risk`，而前端与本文档
> 都用 `risk_level`，结果是筛选被静默忽略、恒返回全量。改参数名时必须
> 同步 `frontend/src/api/index.ts`，否则这类"不报错但没用"的缺陷极难发现。

### 1.5 软删除

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
| GET | `/api/contracts` | 合同列表（大盘页）；支持 `status` / `risk_level` / `business_type` / `keyword` 过滤与分页 |
| GET | `/api/contracts/{id}` | 合同详情（含 `task_id`、`pdf_object_key`） |
| POST | `/api/contracts/upload` | 上传合同（multipart）；`auto_review=true` 时立即启动审查 |
| DELETE | `/api/contracts/{id}` | 软删除 |
| POST | `/api/contracts/batch/delete` | 批量软删除（body: `{"ids": [1,2]}`） |
| GET | `/api/contracts/{id}/clauses` | 条款列表（工作台左栏） |
| GET | `/api/contracts/{id}/metadata` | 提取的元数据（含 `anchors`，供正文高亮字段位置） |
| GET | `/api/contracts/{id}/events` | 任务事件轨迹（审计） |
| GET | `/api/contracts/{id}/parse-results` | 解析历史（重试后 `attempt` 递增） |
| GET | `/api/contracts/{id}/pdf` | **渲染用 PDF**（PDF 返回原件，DOCX / 图片返回产物） |
| GET | `/api/contracts/{id}/file` | 合同原件下载 |

**`/pdf` 为什么必需**：前端统一用 PDF.js 渲染（架构 §14.3），
而 `pdf_object_key` 只是 MinIO 的 key，浏览器无法直接访问。
该接口由后端代理对象存储，前端不直连 MinIO（省掉跨域与凭据配置）。

| 情形 | 行为 |
|---|---|
| PDF | 直接返回原件（**扫描件也走这条**，原件本身就是 PDF） |
| DOCX | 返回 `pdf_object_key` 指向的 WPS COM 转换产物；尚未生成时 **409** |
| 图片 | 返回 OCR 路径包成的单页 PDF（`pdf_object_key`）；尚未生成时 **409** |

> ⚠️ **图片为什么要包成 PDF**：PDF.js 只吃 PDF，图片原件渲染不了。
> 批次 8 起，解析图片时会顺带 `convert_to_pdf()` 并把产物登记到 `pdf_object_key`，
> 否则工作台会显示"正文无法渲染"。扫描件 PDF 不需要这一步。

### 2.3 任务 `tasks`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/progress` | 进度（前端轮询；优先读 Redis，回退数据库） |
| POST | `/api/tasks/{id}/retry` | 重试阻塞任务（仅 `blocked` 可重试，否则 409） |
| POST | `/api/tasks/batch/retry` | 批量重试（body: `{"ids": [合同 ID...]}`，只认 `blocked`） |
| POST | `/api/tasks/sync-todos` | 从审批系统拉待办建合同 + 任务 |

#### 2.3.1 批量操作（PRD 2.4.5）

```jsonc
// POST /api/contracts/batch/delete
// POST /api/tasks/batch/retry   （注意：这里传的是**合同 ID**，不是任务 ID）
{"ids": [469, 470]}
```

响应统一为 `BatchResultOut`：

```json
{
  "total": 2, "succeeded": 1, "failed": 1,
  "results": [
    {"id": 469, "ok": true,  "detail": "已重试"},
    {"id": 470, "ok": false, "detail": "非阻塞状态（completed），跳过"}
  ]
}
```

**三条设计约束**：

1. **逐条处理、逐条回报，不整批回滚**——用户勾了 10 条只成功 9 条时，
   必须知道是**哪一条**没成，否则只能整批重来。
2. **入参是显式 id 列表，不支持"按筛选条件批量"**——筛选条件下批量操作
   会误伤：用户看到的是一页结果，实际影响的是全库匹配项。
3. **`ids` 有界**（`1 ≤ len ≤ 200`，超限 422）——避免一次请求打满数据库连接。

### 2.4 风险 `risks`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/risks?contract_id=` | 风险清单（含锚点、依据链与 `clause_content`，一次查完避免 N+1） |
| GET | `/api/risks/{id}` | 风险详情 |
| PATCH | `/api/risks/{id}` | 法务编辑建议 / 标记采纳 |
| POST | `/api/risks/annotations` | 新增法务批注 |
| GET | `/api/risks/annotations/list?contract_id=` | 批注列表 |

**不变量 I4**：`suggestion_edited` 非空 ⟹ `adopted = 1`。

**`clause_content` 为什么随风险项一起下发**：工作台的"条款差异对比"（PRD 2.4.5）
要展示"原文 ↔ 建议"。若前端另拉 `/clauses` 再按 `clause_id` 反查，
每个合同都会多一次请求且需在前端维护索引；一次查完更省。
后端在 PATCH 里显式维护，调用方不必传对：

```jsonc
// 只传编辑版建议，后端自动补 adopted=true
{"suggestion_edited": "建议改为：验收合格后 15 日内付款。"}

// 空串表示清空（回退到 AI 原始建议）
{"suggestion_edited": ""}
```

### 2.5 规则库 `rules`（批次 7 起可维护）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/rules/options` | 规则配置页下拉选项（枚举 + 中文标签，由后端下发） |
| GET | `/api/rules/templates` | 规则模板列表 |
| PATCH | `/api/rules/templates/{id}` | 更新模板（名称 / 描述 / 启用） |
| GET | `/api/rules/rules` | 规则列表 |
| GET | `/api/rules/rules/{id}` | 规则详情 |
| POST | `/api/rules/rules` | 新建规则（**写入前校验**，非法配置 400） |
| PATCH | `/api/rules/rules/{id}` | 更新规则（未提供的字段不改；`conditions` 提供则整体替换） |
| DELETE | `/api/rules/rules/{id}` | 删除规则（被历史依据引用时 409，需 `force=true`） |
| GET | `/api/rules/standard-clauses` | 示范条款库 |
| POST | `/api/rules/standard-clauses` | 新建示范条款 |
| PATCH | `/api/rules/standard-clauses/{id}` | 更新示范条款 |
| DELETE | `/api/rules/standard-clauses/{id}` | 删除示范条款 |
| GET | `/api/rules/blacklist` | 主体黑名单（⚠️ mock 数据，非真实工商信息） |

#### 2.5.1 为什么写入路径必须校验

规则引擎遇到配错的规则会 `logger.error` 后 `continue`——这是**正确的运行时行为**
（单条坏规则不该中断整轮审查），但后果是**配错的规则永久静默失效**：
用户在配置页看到"已启用"，审查却从不命中，没有任何提示。

因此 `POST/PATCH` 会先过 `services/review/rule_config.validate_rule`，
把"引擎会忽略"的配置在保存时变成 400：

| 校验点 | 为什么 |
|---|---|
| 条件字段前缀必须是 `clause.` / `metadata.` | 引擎按前缀分派，写别的等于没写 |
| `clause.` 后只能是 `content` / `clause_type` / `clause_no` / `title` | 引擎只读这四个键。`clause.payment.content` 这类直觉写法**会被静默忽略**——限定条款类型要用 `config.within_clause_type` + `required_pattern` |
| `metadata.` 后必须是已知元数据键 | 同上 |
| `exists` / `not_exists` 不接受 `value` | 存在性判断只看字段有无 |
| 正则必须能编译 | 引擎运行期会吞掉非法正则，保存时不拦就是永久不生效 |
| 各规则类型的必填参数 | 如 threshold 必须有 `metric` + `threshold` |

**`/options` 为什么由后端下发**：枚举值前后端漂移时用户能选到后端不认的值，
保存后规则静默失效（与列表页 `risk_level` 参数名漂移同类问题）。集中下发杜绝。

#### 2.5.2 为什么规则不做物理删除

`risk_evidence.rule_id` 是 `ON DELETE SET NULL`。硬删规则会让历史审查结果的
"依据链"失去来源——报告里那条依据就说不清是哪条规则判的。
因此被引用时返回 409 并建议改用 `enabled=false`，只有显式 `force=true` 才强删。

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
| `source` | `native_text`（有文本层）/ `ocr`（扫描件、图片）。**文档级取值**，不逐页混源 |
| `confidence` | OCR 页置信度（0~1）；`native_text` 为 `null`。前端据此提示"识别定位，可能有偏差" |

> ⚠️ **OCR 锚点的坐标是近似的**：RapidOCR 只给行框，字符框按行内等宽切分算出。
> 风险项能准确定位到行/条款，但框的左右边界可能与实际字形有偏差。

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
