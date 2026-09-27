# 合同审批审查系统

智能合同审查：从审批系统拉取合同 → 解析（DOCX / PDF）→ 条款切分 →
规则 + LLM 双引擎审查 → 风险清单与原文双向高亮 → 生成审查报告 → 回写审批系统。

> 需求见 `PRD.md`，架构见 `docs/architecture.md`，开发规范见 `AGENTS.md`。

## 1. 架构一览

```
┌────────────────────────── 原生 Windows ──────────────────────────┐
│                                                                  │
│  frontend/  Vite + React + Ant Design + PDF.js     :5173         │
│      │  代理 /api                                                │
│      ▼                                                           │
│  backend/   FastAPI                                :8000         │
│      │  ├── 解析（PyMuPDF / python-docx / WPS COM）              │
│      │  ├── 审查（规则引擎 + LLM + 取高合并 + 防幻觉闸门）        │
│      │  ├── 后台任务（进程内线程 + DB 状态机，不用 Celery）       │
│      │  └── 适配层 ApprovalSystemAdapter                         │
│      │                                                           │
│  mock-approval/  FastAPI 桩（扮演外部审批系统）     :8010         │
│                                                                  │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────┴─── Docker Desktop · WSL2 ─────────────┐
│  cr-mysql :13306     cr-redis :16379     cr-minio :19000         │
└──────────────────────────────────────────────────────────────────┘
```

**为什么后端/前端原生跑、只把基础设施容器化**：
DOCX 分页依赖 WPS COM，需要交互式桌面会话，容器里跑不了（`docs/architecture.md` §5.4、R1）。

## 2. 快速开始

### 2.0 一键启动（推荐）

```powershell
python scripts\start_all.py --open    # 起基础设施 → 建表播种 → 三个服务 → 打开浏览器
```

脚本按依赖顺序执行并逐项探活，已就绪的环节自动跳过，可反复执行。
停止：`python scripts\stop_all.py`（加 `--infra` 连容器一起停）。

下面是分步说明，便于排查单点问题。

### 2.1 前置

| 项 | 要求 |
|---|---|
| Python | 3.12+（虚拟环境在 `backend/.venv`） |
| Node | 18+（本项目实测 24.20.0） |
| Docker Desktop | 跑 MySQL / Redis / MinIO |
| WPS Office | DOCX 转 PDF 分页用（**必需**，否则 DOCX 走无分页降级） |

### 2.2 启动基础设施

```powershell
docker compose up -d
docker ps        # 期望看到 cr-mysql / cr-redis / cr-minio 三个 healthy
```

### 2.3 初始化数据库

```powershell
cd backend
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head   # 建表
cd ..
backend\.venv\Scripts\python.exe scripts\seed_data.py               # 规则库 / 示范条款 / 黑名单
```

### 2.4 启动 mock 审批系统

```powershell
cd mock-approval
..\backend\.venv\Scripts\python.exe seed.py            # 写入演示待办（幂等）
..\backend\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

### 2.5 启动后端

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动时会打印四项自检（MySQL / Redis / MinIO / 审批系统）。
**自检失败不阻断启动**：某个依赖没起时仍能打开 `/docs` 看错误提示。

接口文档：<http://127.0.0.1:8000/docs>

### 2.6 启动前端

```powershell
cd frontend
npm install
npm run dev
```

打开 <http://127.0.0.1:5173>。

## 3. 演示流程

1. 打开大盘页 → 点 **「同步审批待办」**（从 mock 拉取合同并自动开始审查）
2. 等待状态变为 **已完成**（列表自动轮询刷新）
3. 点合同名进入 **工作台**
4. 右栏点风险卡片 → 左栏 PDF 自动跳页并高亮对应条款
5. 左栏点高亮框 → 右栏对应卡片滚动到可视区
6. 卡片上展开 **条款差异对比** 看原文 ↔ 建议逐字增删；点 **复制** 取走修改建议
7. 在卡片上 **编辑建议** / **采纳**
8. 底部填写法务批注 → **保存批注**
9. 点 **预览报告** 看 Markdown 效果，点 **导出** 落存储并下载
10. 点 **写回审批意见** → 写回 mock 审批系统评论区

> 左栏正文里**虚线蓝框**是提取到的元数据字段（甲方 / 合同金额 / 合同编号…），
> 与**半透明实底**的风险高亮视觉区分：前者是"提取到的信息"，后者是"发现的问题"。

也可以直接在大盘页 **上传合同**（`.docx` / `.pdf`）走同一条链路。

### 3.1 大盘页操作（批次 6）

```
┌────────────────────────────────────────────────────────────────────┐
│ 合同审查大盘        [搜索][状态][风险] 刷新 同步审批待办 [上传合同] │
├────────────────────────────────────────────────────────────────────┤
│ ☑ 已选 2 项      [取消选择] [批量重试] [批量删除]     ← 勾选后出现  │
├──┬──────────────┬────────┬──────┬────────┬──────┬─────────────────┤
│☑ │ 合同名称      │ 申请人 │ 金额 │ 审查状态│ 风险 │ 操作            │
├──┼──────────────┼────────┼──────┼────────┼──────┼─────────────────┤
│☐ │ 设备采购合同… │ 张三   │ ¥…   │ 阻塞 ⓘ │ 🔴   │ 工作台 重试 📄 ⬇ 删除│
│  │              │        │      │  ↑悬停看阻塞原因            │
│  │              │        │      │  📄=导出记录弹窗            │
├──┴──────────────┴────────┴──────┴────────┴──────┴─────────────────┤
│                                    本页合计        ¥4,598,000.00   │
└────────────────────────────────────────────────────────────────────┘
```

- **按风险等级筛选**：右上角「风险等级」下拉，走 `risk_level` 参数
- **行内重试**：`阻塞` 行才出现「重试」，悬停状态标签可看阻塞原因（PRD 2.4.7）
- **批量操作**：勾选任意行后出现操作条；**部分失败会逐条列出是哪几条**
- **导出记录**：点行内 📄 图标看该合同的报告导出历史（含大小 / 导出人 / 下载）

> 批量重试传的是**合同 ID**；非 `blocked` 的条目会逐条回报"非阻塞状态，跳过"，
> 不会静默忽略。

## 4. 环境变量

复制 `.env.example` 为 `.env` 后按需修改。关键项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MYSQL_PORT` | `13306` | 容器映射端口（**不碰本机 3306**） |
| `REDIS_PORT` | `16379` | 容器映射端口 |
| `MINIO_PORT` | `19000` | 容器映射端口 |
| `LLM_PROVIDER` | `mock` | `mock` 或 `openai_compat` |
| `LLM_BASE_URL` | — | OpenAI 兼容端点；`openai_compat` 时必填 |
| `LLM_API_KEY` | — | 同上 |
| `LLM_MODEL` | — | 模型名 |
| `DOCX_CONVERTER` | `wps_com` | DOCX → PDF 转换器 |

**无 LLM Key 也能完整演示**：`LLM_PROVIDER=mock` 时用预置结论回放，
整条链路（解析 → 审查 → 落库 → 报告 → 回写）依然走通。

自检环境：`backend\.venv\Scripts\python.exe scripts\check_env.py`

## 5. 测试

```powershell
# 单元 + 集成测试（需基础设施已启动）
backend\.venv\Scripts\python.exe -m pytest backend\tests -q

# 数据一致性检查 C1~C8
backend\.venv\Scripts\python.exe scripts\check_consistency.py

# 回归断言集：跑示例合同，比对人工标注的期望风险点
backend\.venv\Scripts\python.exe scripts\run_tests.py

# 加上锚点坐标校验（用真实渲染的 PDF + PDF.js 实测高亮位置）
backend\.venv\Scripts\python.exe scripts\run_tests.py --verify-anchors
```

前端：

```powershell
cd frontend
npx tsc --noEmit     # 类型检查
npx vite build       # 生产构建
```

> ⚠️ **不要并行执行上述命令**：`pytest`、`check_consistency.py`、`run_tests.py`
> 都会清空业务表（`contract` / `review_task` 及其下游），并行跑会互相踩踏。

### 测试分层

| 层 | 位置 | 覆盖什么 |
|---|---|---|
| 数据层 | `backend/tests/test_models.py` | 模型约束、生成列去重、MySQL 专有行为 |
| 引擎层 | `backend/tests/test_review_engine.py` | 状态机、锚点对齐、取高合并、防幻觉闸门、端到端流水线 |
| 接口层 | `backend/tests/test_api.py` | HTTP 契约、错误路径、不变量维护、软删除 |
| 质量回归 | `scripts/run_tests.py` | 示例合同的期望风险点是否被识别、等级与锚点是否正确 |

## 6. 目录结构

```
contract-approval-and-review-system/
├── PRD.md                     需求文档
├── AGENTS.md                  开发规范
├── compose.yml                基础设施编排（mysql / redis / minio）
├── .env / .env.example        环境变量
│
├── docs/
│   ├── architecture.md        架构设计（主文档）
│   ├── data-model.md          数据对象设计
│   ├── data-layer-guide.md    数据层使用说明
│   ├── review-engine.md       审查引擎说明
│   ├── api-guide.md           后端 API 手册
│   └── mock-approval.md       mock 审批系统说明
│
├── backend/                   FastAPI · 原生运行
│   ├── app/
│   │   ├── main.py            应用入口
│   │   ├── api/               路由层（只做编排）
│   │   ├── core/              配置 / 数据库 / Redis / MinIO 客户端
│   │   ├── models/            SQLAlchemy ORM（18 张表）
│   │   ├── schemas/           Pydantic 对外契约
│   │   ├── services/
│   │   │   ├── parsing/       解析（PDF / DOCX / OCR / 锚点构建）
│   │   │   ├── review/        切分 / 规则引擎 / LLM 审查 / 合并 / 闸门
│   │   │   ├── llm/           LLMProvider 抽象（OpenAICompat / Mock）
│   │   │   ├── approval/      审批系统适配层
│   │   │   ├── report_service.py      报告生成与导出
│   │   │   └── writeback_service.py   回写审批系统
│   │   └── workers/           状态机 + 审查流水线
│   ├── tests/
│   └── alembic/               数据库迁移
│
├── frontend/                  React + Vite · 原生运行
│   ├── src/
│   │   ├── pages/             大盘页 / 工作台
│   │   ├── components/        PDF 视窗 / 风险卡片 / 协同回写栏
│   │   ├── lib/               锚点坐标换算（前后端契约的唯一实现）
│   │   ├── api/               接口封装
│   │   └── types/             与后端 schemas 对应的类型
│   └── scripts/               verify_anchors.mjs（锚点坐标校验）
│
├── mock-approval/             mock 审批系统 · 独立服务
│   ├── app/                   FastAPI 应用（M1~M4）
│   ├── data/                  JSON 存储（待办 / 评论 / 附件）
│   └── seed.py                种子数据脚本
│
├── samples/
│   ├── purchase/              采购合同示例（含高风险用例）
│   └── expected/              人工标注的期望风险点（回归断言集）
│
└── scripts/
    ├── start_all.py           一键启动
    ├── stop_all.py            一键停止
    ├── check_env.py           环境自检
    ├── check_consistency.py   数据一致性检查 C1~C8
    ├── seed_data.py           规则库 / 示范条款 / 黑名单种子
    └── run_tests.py           回归断言集执行器
```

## 7. 已知限制

| 限制 | 说明 |
|---|---|
| WPS COM 依赖交互式桌面 | 后端不能服务化（容器/无桌面会话下 DOCX 分页失效，走无分页降级） |
| WPS 与 Word 分页可能不一致 | 报告标注"页码基于 WPS 排版" |
| 扫描件 OCR 阶段一未启用 | 图片输入返回 409；`RapidOCR` 代码路径已就绪 |
| 合同版本管理未做 | `contract` ↔ `review_task` 1:1 |
| 单进程后台线程 | 演示并发为 1；批量审查需迁 Celery（阶段二） |
| 黑名单是 mock 数据 | 不代表真实工商信息 |
| 风险阈值是拟值 | 非法律意见，可在规则库调整 |

## 8. 故障排查

| 现象 | 排查方向 |
|---|---|
| 启动日志里某项自检 ✗ | 对应容器没起：`docker ps`；或 `.env` 端口与实际映射不符 |
| 上传 DOCX 后一直"解析中" | WPS 是否可正常打开文档；看后端日志的转换器降级链 |
| 任务变 `blocked` | 看工作台"审查轨迹"或 `GET /api/contracts/{id}/events` |
| 正文区提示"正文无法渲染" | 该合同尚无转换后 PDF（`pdf_object_key` 为空）；审查结果仍可看 |
| 风险卡片有"未锚定"标记 | LLM 引用无法定位到原文，属防幻觉闸门正常工作，需人工核查 |
| 回写失败 | 看 `GET /api/contracts/{id}/writeback/status` 的 `error_detail` |
| 前端 `/api` 请求 404 | 后端没起，或 `vite.config.ts` 代理目标端口不对 |
| 端口被占用（8000/8010/5173） | `python scripts\stop_all.py` 收尾；它只杀本项目进程，无关进程会跳过并打印命令行 |
