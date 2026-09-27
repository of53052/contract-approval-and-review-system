# mock 审批系统说明

> 代码位置：`mock-approval/`　｜　端口：`127.0.0.1:8010`
> 设计依据：`docs/architecture.md` §3（M1~M4）、§17（为什么独立于 backend）

## 1. 它扮演什么

真实环境里，合同附件来自 OA / 审批系统，审查结论要写回它的评论区。
阶段一没有真实审批系统，因此自写一个**独立可启动**的桩服务。

**关键约束**：后端只依赖 `ApprovalSystemAdapter` 抽象
（`backend/app/services/approval/base.py`），不依赖这个 mock。
将来换真实系统，只改适配层的实现，`mock-approval/` 整个目录可以删掉。

```
backend ──HTTP──> ApprovalSystemAdapter ──HTTP──> mock-approval
                        ↑
              换真实系统时只替换这里
```

## 2. 四个能力（M1~M4）

| 编号 | 能力 | 接口 |
|---|---|---|
| M1 | 待办拉取 | `GET /api/todos`、`GET /api/todos/{no}`、`POST /api/todos/{no}/status` |
| M2 | 附件下载 | `GET /api/todos/{no}/attachments`、`GET /api/todos/{no}/attachments/{aid}` |
| M3 | 评论写入 | `POST /api/todos/{no}/comments`（**幂等**） |
| M4 | 事件推送 | `POST /api/events/push`（把事件 POST 回后端） |

### 完整接口清单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，返回待办数 |
| GET | `/api/todos` | 待办列表，`?status=` 过滤 |
| GET | `/api/todos/{approval_no}` | 待办详情 |
| POST | `/api/todos/{approval_no}/status` | 更新待办状态 |
| GET | `/api/todos/{approval_no}/attachments` | 附件列表 |
| GET | `/api/todos/{approval_no}/attachments/{attachment_id}` | 下载附件 |
| POST | `/api/todos/{approval_no}/comments` | 写入评论（幂等） |
| GET | `/api/todos/{approval_no}/comments` | 评论列表 |
| POST | `/api/events/push` | 推送事件到后端回调地址 |
| GET | `/api/meta/attachments` | 列出已挂载的附件文件 |

## 3. 幂等：M3 的设计要点

回写是"可能重试"的操作，重复写入会在审批系统里留下多条相同评论。
因此 `POST /comments` 接受 `idempotency_key`：

- 首次调用 → 落库，返回 `deduplicated: false`
- 同 key 再次调用 → **不新增**，返回首次的 `comment_id` 与 `deduplicated: true`

后端侧对应实现见 `backend/app/services/writeback_service.py` 的
`make_idempotency_key()`，key 由 `合同 ID + 回写内容 hash` 生成，
因此"内容没变就复用，内容变了才新写一条"。

## 4. 事件推送：M4 为什么失败不报 500

`POST /api/events/push` 是 mock 主动把事件 POST 给后端。
若后端没启动，**这是正常情况**（联调时可能只起了一边），
因此返回 `delivered: false` + `http: 0`，而不是抛 500：

```json
{"delivered": false, "http": 0, "detail": "连接被拒绝"}
```

这样演示时能清楚看到"推送尝试了但目标不可达"，而不是一个没有信息量的错误。

## 5. 启动与种子数据

```powershell
# 1) 写入演示数据（幂等；重复执行不会清空已写入的评论）
cd mock-approval
..\backend\.venv\Scripts\python.exe seed.py

# 2) 启动服务
..\backend\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

`seed.py` 会：

- 把 `samples/purchase/*.docx` 复制到 `data/attachments/`
- 生成 `data/todos.json`（当前 1 条待办 `AP-2026-0001`）
- **不动** `data/comments.json`（评论是回写演示的产物）

数据文件都是纯 JSON，出问题时可以直接看：

```
mock-approval/data/
├── todos.json          待办清单
├── comments.json       评论（回写产物）
└── attachments/        附件文件
```

## 6. 存储实现

`app/store.py` 用**JSON 文件 + 线程锁**，不引数据库：

- 桩服务的价值在于"零依赖启动"，加个 DB 反而增加演示成本
- 并发写用 `threading.Lock` 保护；uvicorn 单进程多线程下足够
- 评论幂等靠内存 + 文件双写，重启后仍生效

## 7. 与后端的契约测试

`backend/tests/test_api.py::test_sync_todos_requires_approval_service`
验证了"审批系统不可达 → 502"这条错误路径；
四个能力的行为验证见 `docs/api-guide.md` §5 的端到端链路。
