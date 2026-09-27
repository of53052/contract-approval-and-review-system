# 合同审批审查系统 — 架构设计文档

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 状态 | 已定稿，待评审 |
| 适用范围 | 阶段一 MVP（端到端单合同闭环） |
| 决策依据 | 6 轮设计追问 + 本机实测（见附录 A） |

> 本文档中的架构决策**均以实测数据为依据**，而非理论推断。所有实测数字可在
> `docs/probe/` 的脚本中复现。

---

## 1. 系统概述

### 1.1 目标

面向企业合同审批场景的智能审查系统：支持多格式合同文档解析与条款结构化切分，
通过规则引擎与大语言模型识别法律合规与商业风险，提供原文高亮定位、智能修改建议
与报告导出，并将审查意见回写至企业审批系统评论区。

### 1.2 交付物

| # | 交付物 | 形态 |
|---|---|---|
| 1 | 前端 | React 应用（审查大盘 / 智能审查工作台 / 规则模板页） |
| 2 | 后端审查服务 | FastAPI 应用 |
| 3 | 示例合同集 | 采购 / 销售 / 劳动，含标准与风险用例 |
| 4 | 审批回写模拟模块 | 独立 mock 服务 |
| 5 | 完整演示链路 | 至少一个场景端到端 |

### 1.3 项目定位

**演示优先，架构分层清晰，关键模块可替换。**

不做量化准确率指标（不搭带标注的评测集），改为**为每个示例合同人工标注期望风险点**，
做成可回归的断言集。理由：PRD 未提供真实工商数据源与真实审批系统，
生产化的两个前置条件缺失；断言集能以极小成本防止回归。

---

## 2. 关键决策记录

> 每条决策都记录了**被否决的方案**，避免后续反复。

| # | 决策项 | 结论 | 否决方案与理由 |
|---|---|---|---|
| D1 | 部署形态 | 后端/前端/mock **原生**，基础设施**容器化** | 全容器化：与 WPS COM 冲突（COM 需交互式桌面） |
| D2 | 数据库 | **MySQL 8.4 容器**（utf8mb4 + 严格模式） | SQLite：容器化后无优势，双 DB 是纯负担；本机 FlyEnv 实例：20 库共享，非严格模式有静默截断风险 |
| D3 | 对象存储 | **MinIO 容器** | 本地目录：不利于演示"生产形态" |
| D4 | 缓存 | **Redis 容器** | 无缓存：解析进度与 OCR 结果需要缓存 |
| D5 | 文本 PDF 解析 | **PyMuPDF** | pdfplumber / pypdf：实测 PyMuPDF 9.26ms 且支持字符级 bbox |
| D6 | DOCX 分页 | **WPS COM 导出 PDF** | 自研分页（不可能准）；python-docx（无分页信息）；LibreOffice（分页与 WPS 不一致） |
| D7 | 扫描件 OCR | **RapidOCR (onnxruntime)** | PaddleOCR：慢 11 倍且本机有 oneDNN 崩溃 bug |
| D8 | 锚点模型 | **统一 PDF 坐标系** `{page, bbox, char_range?}` | 判别式联合（两套渲染分支）：统一后前端只需一套渲染 |
| D9 | 规则与 LLM 冲突 | **取高不取低**，双方留痕 | 规则优先（太死板）；LLM 优先（不可复现） |
| D10 | LLM 接入 | **Provider 抽象**，默认 Mock | 硬编码某家 SDK：无 Key 时演示跑不起来 |
| D11 | 异步任务 | **DB 状态机 + 进程内后台任务** | Celery + Redis：演示并发量为 1，过度设计 |
| D12 | 审批系统 | **自写 mock 服务** | 真接：PRD 未给任何真实系统信息 |
| D13 | 阶段划分 | **两阶段**，先打穿单合同闭环 | 一次做全：PRD 只要求"至少一个场景" |

---

## 3. 整体架构

```mermaid
flowchart TB
    subgraph ACTOR["用户角色"]
        A1["法务审查人"]
        A2["业务经办人"]
        A3["系统管理员"]
    end

    subgraph FE["前端 · React 18 + TS + Vite · 原生运行"]
        F1["审查大盘页"]
        F2["智能审查工作台"]
        F3["合规规则模板页"]
    end

    subgraph BE["后端 · FastAPI · 原生 Windows"]
        B1["接入与流转模块"]
        B2["文档解析引擎"]
        B3["智能审查引擎"]
        B4["条款推荐引擎"]
        B5["审批回写模块"]
    end

    subgraph MOCK["mock 审批服务 · 容器"]
        M1["待办拉取"]
        M2["附件下载"]
        M3["评论写入"]
        M4["事件推送"]
    end

    subgraph INFRA["基础设施 · Docker"]
        I1["MySQL 8.4"]
        I2["Redis 8"]
        I3["MinIO"]
    end

    subgraph HOST["宿主能力 · 仅 Windows 可用"]
        H1["WPS COM"]
        H2["PyMuPDF"]
        H3["RapidOCR"]
    end

    A1 --> F2
    A2 --> F1
    A3 --> F3
    F1 --> B1
    F2 --> B3
    F2 --> B5
    F3 --> B1

    B1 --> M1
    B1 --> M2
    B5 --> M3
    M4 --> B1

    B2 --> H1
    B2 --> H2
    B2 --> H3

    B1 --> I1
    B2 --> I2
    B2 --> I3
    B3 --> I1
    B5 --> I1
```

**分层职责**

| 层 | 职责 | 关键约束 |
|---|---|---|
| 前端 | 交互与渲染 | 统一 PDF.js 渲染，一套坐标系统 |
| 后端 | 编排与业务 | 原生运行，可调用宿主 COM |
| mock 审批 | 扮演外部系统 | 与后端**不共享代码**，仅通过 HTTP 契约耦合 |
| 基础设施 | 数据与存储 | 容器化，端口与宿主实例错开 |
| 宿主能力 | 重依赖解析 | Windows 专有，**不可容器化** |

---

## 4. 部署架构

```mermaid
flowchart LR
    subgraph WIN["Windows 宿主"]
        direction TB
        subgraph NATIVE["原生进程"]
            N1["FastAPI :8000"]
            N2["mock 审批 :8010"]
            N3["Vite dev :5173"]
        end
        subgraph HOSTCAP["宿主能力调用"]
            C1["WPS COM"]
            C2["PyMuPDF"]
            C3["RapidOCR"]
        end
        N1 -.-> C1
        N1 -.-> C2
        N1 -.-> C3
    end

    subgraph DOCKER["Docker Desktop · WSL2"]
        direction TB
        D1["cr-mysql :13306"]
        D2["cr-redis :16379"]
        D3["cr-minio :19000 / :19001"]
    end

    N1 -->|"TCP"| D1
    N1 -->|"TCP"| D2
    N1 -->|"HTTP"| D3
    N2 --> D1
    N3 -->|"HTTP"| N1
```

**端口规划**（刻意错开本机 FlyEnv 实例，两套并存互不干扰）

| 服务 | 本项目端口 | 本机 FlyEnv 端口 | 说明 |
|---|---|---|---|
| MySQL | `13306` | `3306` | 隔离，避免污染 20 个既有库 |
| Redis | `16379` | `6379` | 隔离 |
| MinIO | `19000` / `19001` | — | API / Console |
| 后端 | `8000` | — | 原生 |
| mock 审批 | `8010` | — | 原生 |
| 前端 dev | `5173` | — | Vite |

**容器资源实测**（WSL2 默认配额）

| 项 | 值 |
|---|---|
| 可用 CPU | 12 核（Ryzen 5 5600H 全部） |
| 可用内存 | 7.87 GB（宿主 15.86 GB 的 50%） |
| 启动耗时 | 19 秒达 3/3 healthy |

**一键启动**：`python scripts/start_all.py`（`--open` 顺带开浏览器）。
按"前置检查 → 容器 healthy 等待 → alembic 建表 → 种子数据 → 三个服务探活"顺序执行，
已就绪的环节自动跳过（幂等）。停止用 `python scripts/stop_all.py`（`--infra` 连容器一起停）。

> 停止脚本只终止 `.run/*.json` 记录过的 PID；记录缺失时按端口反查，
> 且要求命令行同时含 `uvicorn`/`vite` 与 `--port <端口>` 才认定为本项目进程，
> 避免误杀恰好占用同端口的无关服务。

---

## 5. 文档解析引擎

### 5.1 四条输入路径

```mermaid
flowchart LR
    IN1["DOCX"] --> R1{"格式判定"}
    IN2["文本型 PDF"] --> R1
    IN3["扫描件 PDF"] --> R1
    IN4["图片"] --> R1

    R1 -->|"DOCX"| P1["WPS COM<br/>导出 PDF"]
    R1 -->|"文本 PDF"| P2["PyMuPDF<br/>直接提取"]
    R1 -->|"扫描 PDF / 图片"| P3["PyMuPDF 渲染<br/>+ RapidOCR"]

    P1 --> U["统一段落树<br/>+ 元数据"]
    P2 --> U
    P3 --> U
    U --> A["统一锚点<br/>PDF 坐标系"]
```

### 5.2 各路径实测数据

| 路径 | 方案 | 耗时 | 页码来源 | 字符级 bbox |
|---|---|---|---|---|
| 文本 PDF | PyMuPDF `get_text("dict")` | **9.26 ms** / 3 页 | `page.number` | ✅ `rawdict` 的 `span["chars"]` |
| DOCX | WPS COM → PDF | **1.41 s**（启动0.82+加载0.34+导出0.25） | WPS 排版结果 | ✅ 同文本 PDF |
| 扫描件 | 渲染 200 DPI + RapidOCR | **6.08 s** / 页 | `page.number` | ✅ 4 点多边形 |
| 图片 | 先包成 PDF 再走扫描件路径 | 同上 | 同上 | ✅ |

### 5.3 关键约束

> ⚠️ **DOCX 文件本身不含分页信息。**
> 证据：`<w:pageBreak>` = 0、`<w:lastRenderedPageBreak>` = 0、python-docx 无页码 API。
> 真实页码必须由排版引擎渲染后才可知。

> ⚠️ **WPS 与 LibreOffice 分页结果不一致。**
> 实测同一份 DOCX：WPS = 3 页，LibreOffice = 2 页（字体度量差异导致断行不同）。
> **本项目以 WPS 为准**，因为它是本机实际使用的办公软件，用户所见即所得。

> ⚠️ **OCR 会丢失表格结构。**
> 实测扫描件表格页识别为 `4自动输送线CL-X1201152,000.00152,000.00`（数字粘连）。
> 对策：扫描件来源的元数据**打置信度标记**，UI 提示"OCR 识别，请人工核对"。

### 5.4 DOCX 转换器可插拔设计

WPS COM 依赖**交互式桌面会话**，若后端跑成 Windows 服务（无桌面）会失败。
因此设计为策略模式：

```mermaid
classDiagram
    class DocxConverter {
        <<interface>>
        +to_pdf(path) bytes
        +is_available() bool
    }
    class WpsComConverter {
        +to_pdf(path) bytes
        +is_available() bool
    }
    class LibreOfficeConverter {
        +to_pdf(path) bytes
        +is_available() bool
    }
    class PassthroughConverter {
        +to_pdf(path) bytes
        +is_available() bool
    }
    DocxConverter <|.. WpsComConverter
    DocxConverter <|.. LibreOfficeConverter
    DocxConverter <|.. PassthroughConverter
```

| 实现 | 可用条件 | 分页语义 |
|---|---|---|
| `WpsComConverter` | Windows + WPS + 交互式桌面 | WPS 排版（**首选**） |
| `LibreOfficeConverter` | 装了 LibreOffice | LO 排版（与 WPS 可能不同） |
| `PassthroughConverter` | 总是可用 | **无分页**，降级为段落级锚点 |

**降级链**：`WPS COM` → `LibreOffice` → `Passthrough`（段落级）。
任一环节失败时记录原因，**不静默降级**。

---

## 6. 统一锚点模型

### 6.1 设计原则

四条输入路径全部收敛到 **PDF 坐标系**，前端只需**一套渲染路径**。

```
DOCX ──(WPS COM)──┐
文本 PDF ──────────┼──→ PDF ──→ { page, bbox } + 可选 char_range
扫描 PDF ──────────┤            ↑
图片 ──(包成PDF)───┘      前端 PDF.js 统一渲染
```

### 6.2 数据结构

```json
{
  "page": 1,
  "bbox": [189.5, 58.96, 405.5, 82.7],
  "char_range": [0, 12],
  "source": "native_text",
  "confidence": 1.0
}
```

| 字段 | 必需 | 说明 |
|---|---|---|
| `page` | ✅ | 页码，从 1 开始 |
| `bbox` | ✅ | `[x0, y0, x1, y1]`，**PDF point 坐标**（非像素） |
| `char_range` | ❌ | 文本层中的字符区间；**扫描件为 `null`** |
| `source` | ✅ | `native_text` / `ocr` |
| `confidence` | ❌ | OCR 时为识别置信度，文本层为 `1.0` |

**关键点**：`bbox` 是唯一必需字段，`char_range` 可空。这解决了
"扫描件没有字符层"与"PRD 要求记录字符位置"之间的矛盾。

### 6.3 坐标映射（已实测验证）

```
OCR 像素坐标 × (72 / DPI) = PDF point 坐标
```

实测误差（OCR 框左上角 vs 原生文本行框左上角）：

| 文字 | OCR 像素 | 换算 PDF 坐标 | 原生 search_for | 误差 (pt) |
|---|---|---|---|---|
| 设备采购合 | (527.1, 178.0) | (189.8, 64.1) | (189.5, 59.0) | (0.3, 5.1) |
| 合同编号： | (475.5, 264.1) | (171.2, 95.1) | (171.0, 92.9) | (0.2, 2.2) |
| 甲方（采购 | (180.8, 326.6) | (65.1, 117.6) | (64.0, 114.4) | (1.1, 3.2) |

误差 0.2~5.1 pt，属 OCR 检测框 padding 的正常范围，**映射成立**。

### 6.4 引用对齐（LLM 输出 → 锚点）

LLM 返回的原文引用可能改标点、省略号、全半角混用，因此采用三级策略：

```mermaid
flowchart TD
    S["LLM 引用片段"] --> N["文本归一化<br/>全半角 / 标点 / 空白"]
    N --> M1{"① 精确匹配<br/>PyMuPDF search_for"}
    M1 -->|命中| OK["锚定成功<br/>page + bbox"]
    M1 -->|未命中| M2{"② 模糊匹配<br/>归一化 + 相似度阈值"}
    M2 -->|命中| OK
    M2 -->|未命中| M3["③ 降级到条款级锚点<br/>标记 anchor_level=paragraph"]
    M3 --> W["标记 unanchored=true<br/>UI 提示无法定位"]
    OK --> END["写入风险项"]
    W --> END
```

**核心原则**：引用无法定位时**必须显式标记**，绝不静默丢弃，也不伪造位置。
这是验收标准"原文与风险卡片双向高亮跳转"的兜底。

---

## 7. 任务状态机

### 7.1 任务状态

```mermaid
stateDiagram-v2
    [*] --> pending : 任务创建
    pending --> parsing : 开始解析
    parsing --> reviewing : 解析成功
    parsing --> blocked : 加密 / 空白 / 严重模糊
    reviewing --> completed : 审查完成
    blocked --> parsing : 人工重试
    completed --> [*]

    note right of blocked
        记录阻塞原因
        支持重新上传或一键重试
    end note
```

| 状态 | 含义 | 进入条件 |
|---|---|---|
| `pending` | 待处理 | 任务创建 |
| `parsing` | 解析中 | 开始解析（CPU OCR 为分钟级） |
| `reviewing` | 审查中 | 解析成功，进入规则+LLM 审查 |
| `blocked` | 解析受阻 | 文档加密 / 正文为空 / 扫描件严重模糊 |
| `completed` | 审查完成 | 审查完成，风险项已落库 |

### 7.2 回写状态

```mermaid
stateDiagram-v2
    [*] --> not_written : 审查完成
    not_written --> writing : 触发回写
    writing --> success : 接口返回成功
    writing --> failed : 接口异常 / 超时
    failed --> writing : 重试
    success --> [*]
```

### 7.3 阻塞超时策略

> ⚠️ 本地 CPU OCR 为**分钟级**，超时阈值**不能写死**。

```
timeout = base_timeout + per_page_timeout × page_count
```

| 参数 | 建议值 | 依据 |
|---|---|---|
| `base_timeout` | 60 s | 文件下载 + 引擎初始化（RapidOCR 初始化实测 2.40 s） |
| `per_page_timeout` | 30 s | 单页 OCR 实测 6.08 s，留 5× 余量应对复杂版面 |

同时解析过程**上报真实进度**（`已处理 3/12 页`），避免前端干等。

---

## 8. 智能审查引擎

### 8.1 双引擎协作

```mermaid
flowchart TB
    IN["条款 + 元数据"] --> RULE["规则引擎<br/>确定性判定"]
    IN --> LLM["LLM 语义研判<br/>权责对等性"]

    RULE --> RR["规则风险项<br/>可复现 / 可解释"]
    LLM --> LR["LLM 风险项<br/>灵活 / 需校验"]

    RR --> MERGE{"合并策略"}
    LR --> MERGE
    MERGE -->|"取高不取低"| FINAL["最终风险项<br/>双来源留痕"]
```

### 8.2 职责边界

| 引擎 | 负责 | 特点 |
|---|---|---|
| **规则引擎** | 金额、违约金比例、管辖地关键词、必备条款存在性、主体黑名单 | 可复现、可解释、能进报告当"法律依据" |
| **LLM** | 权责是否对等、表述是否高危、条款语义冲突 | 灵活，但需防幻觉 |

### 8.3 冲突合并策略：取高不取低

> **依据**：法律审查场景中"漏报"比"误报"代价大得多，取高是安全侧；
> 同时两个来源都留痕，报告里能写清"规则命中 + AI 研判"两条依据，可解释性保住。

示例：

```
合同写「违约金为合同总额的 25%」
  → 规则表阈值 20% → 规则判 中风险
  → LLM 读到上下文（我方弱势 + 对方已逾期 3 次）→ LLM 判 高风险
  → 最终 = 高风险（取高），报告呈现两条依据
```

### 8.4 风险等级判定

```mermaid
flowchart TD
    C["条款内容"] --> H{"命中高风险规则?"}
    H -->|是| RH["高风险"]
    H -->|否| M{"命中中风险规则?"}
    M -->|是| RM["中风险"]
    M -->|否| RL["低风险"]
    RH --> MERGE["与 LLM 结果取高"]
    RM --> MERGE
    RL --> MERGE
```

**高风险触发条件**（任一命中）

- 主体信息缺失或被列入经营异常
- 违约责任严重不对等（如我方单方承担无限责任）
- 管辖地约定违规
- 未设置付款前置验收条件

**中风险触发条件**（任一命中）

- 缺少保密义务期限
- 未明确不可抗力通知时效
- 违约金比例超出阈值

### 8.5 可配置阈值表

> ⚠️ **以下为演示用拟值，不是法律意见。实际使用必须由法务校准。**

| 规则项 | 阈值 / 判定清单 | 风险等级 |
|---|---|---|
| 违约金比例上限 | 20%（PRD 举例值，保守） | 超限 → 中风险 |
| 管辖地违规清单 | 境外仲裁机构 / 约定对方所在地法院 / 排除我方所在地管辖 | 命中 → 高风险 |
| 付款前置验收 | 必须存在"验收"相关条款 | 缺失 → 高风险 |
| 保密义务期限 | 必须明确期限 | 缺失 → 中风险 |
| 不可抗力通知时效 | 必须明确时效 | 缺失 → 中风险 |

### 8.6 必备条款全局校验

> ⚠️ **长合同分块后，"必备条款缺失"是全局判断**，单块审查天然看不见。

因此审查分两层：

```mermaid
flowchart LR
    DOC["完整合同"] --> SPLIT["分块"]
    SPLIT --> C1["块 1 审查"]
    SPLIT --> C2["块 2 审查"]
    SPLIT --> C3["块 N 审查"]
    DOC --> GLOBAL["全局清单校验<br/>必备条款存在性"]
    C1 --> MERGE["汇总"]
    C2 --> MERGE
    C3 --> MERGE
    GLOBAL --> MERGE
```

### 8.7 防幻觉闸门

```mermaid
flowchart TD
    LLM["LLM 输出 JSON"] --> V1{"JSON 可解析?"}
    V1 -->|否| RETRY["重试 / 提示词修正"]
    V1 -->|是| V2{"引用片段可锚定?"}
    V2 -->|否| UN["标记 unanchored=true<br/>UI 提示无法定位"]
    V2 -->|是| V3{"法律依据在知识库内?"}
    V3 -->|否| WARN["标记 依据待人工复核"]
    V3 -->|是| OK["风险项有效"]
    UN --> SAVE["落库"]
    WARN --> SAVE
    OK --> SAVE
```

**三条硬规则**

1. **JSON 解析失败必须重试或报错**，不能返回半成品
2. **引用无法锚定的风险项必须标记**，不能当作正常结果
3. **法律依据要么限定知识库选取，要么显式标注"AI 生成，需人工复核"**

---

## 9. LLM Provider 抽象

### 9.1 接口设计

```mermaid
classDiagram
    class LLMProvider {
        <<interface>>
        +chat(messages, **kw) str
        +chat_json(messages, schema) dict
        +is_available() bool
    }
    class OpenAICompatProvider {
        +base_url str
        +api_key str
        +model str
        +chat(messages, **kw) str
        +chat_json(messages, schema) dict
    }
    class MockProvider {
        +fixtures dict
        +chat(messages, **kw) str
        +chat_json(messages, schema) dict
    }
    LLMProvider <|.. OpenAICompatProvider
    LLMProvider <|.. MockProvider
```

### 9.2 为什么只用两个实现

`OpenAICompatProvider` 用 `openai` 库的自定义 `base_url`，**一套代码兼容**：
DeepSeek / 通义 / 智谱 / Kimi / 硅基流动 / 自建 vLLM / Ollama。

**不引入额外 SDK**，符合 AGENTS"禁止擅自引入新第三方依赖"。

### 9.3 配置

```env
LLM_PROVIDER=mock              # mock | openai_compat
LLM_BASE_URL=                  # 如 https://api.deepseek.com/v1
LLM_API_KEY=
LLM_MODEL=
```

**默认 `mock`**：无 Key 时演示仍能完整跑通全链路。
`MockProvider` 回放预置审查结果（带完整依据链），UI 明确标注「模拟输出」。

### 9.4 JSON 输出兜底

> ⚠️ 若接口不支持 `response_format: {"type": "json_object"}`，
> 需要「JSON 提取 + 校验 + 重试」三层兜底，不能让 LLM 输出格式把整条链路搞挂。

```mermaid
flowchart LR
    P["提示词约束<br/>要求输出 JSON"] --> C1["原生 JSON 模式<br/>若支持"]
    C1 --> C2["正则提取 JSON 代码块"]
    C2 --> C3["schema 校验<br/>Pydantic"]
    C3 -->|失败| C4["重试 + 错误反馈<br/>最多 N 次"]
    C3 -->|成功| OUT["结构化结果"]
    C4 --> OUT
```

---

## 10. 数据模型

### 10.1 ER 图

```mermaid
erDiagram
    CONTRACT ||--o{ CLAUSE : "包含"
    CONTRACT ||--o{ RISK_ITEM : "产生"
    CONTRACT ||--|| REVIEW_TASK : "对应"
    CLAUSE ||--o{ RISK_ITEM : "命中"
    RISK_ITEM ||--o| ANCHOR : "定位"
    RISK_ITEM ||--o{ RISK_SOURCE : "依据"
    CONTRACT ||--o{ ANNOTATION : "批注"
    CONTRACT ||--o{ WRITEBACK_LOG : "回写记录"
    CONTRACT ||--o{ EXPORT_RECORD : "导出记录"
    RULE ||--o{ RULE_CONDITION : "包含"
    RULE_TEMPLATE ||--o{ RULE : "归类"

    CONTRACT {
        bigint id PK
        varchar title
        varchar contract_no
        varchar business_type
        decimal amount
        varchar currency
        varchar applicant
        varchar counterparty
        varchar counterparty_code
        varchar file_path
        varchar file_hash
        varchar file_format
        datetime created_at
    }
    REVIEW_TASK {
        bigint id PK
        bigint contract_id FK
        varchar status
        varchar writeback_status
        varchar blocked_reason
        int total_pages
        int parsed_pages
        decimal overall_risk
        datetime created_at
        datetime updated_at
    }
    CLAUSE {
        bigint id PK
        bigint contract_id FK
        varchar clause_type
        varchar clause_no
        text content
        int page_no
        int para_index
    }
    RISK_ITEM {
        bigint id PK
        bigint contract_id FK
        bigint clause_id FK
        varchar title
        varchar risk_level
        varchar category
        text reason
        text legal_basis
        text suggestion
        varchar anchor_level
        boolean unanchored
        datetime created_at
    }
    ANCHOR {
        bigint id PK
        bigint risk_item_id FK
        int page_no
        float bbox_x0
        float bbox_y0
        float bbox_x1
        float bbox_y1
        int char_start
        int char_end
        varchar source
        float confidence
    }
    RISK_SOURCE {
        bigint id PK
        bigint risk_item_id FK
        varchar source_type
        varchar detail
    }
    ANNOTATION {
        bigint id PK
        bigint contract_id FK
        varchar author
        text content
        datetime created_at
    }
    WRITEBACK_LOG {
        bigint id PK
        bigint contract_id FK
        varchar status
        text payload
        text response
        int retry_count
        datetime created_at
    }
    RULE_TEMPLATE {
        bigint id PK
        varchar contract_type
        varchar name
    }
    RULE {
        bigint id PK
        bigint template_id FK
        varchar name
        varchar risk_level
        varchar rule_type
        boolean enabled
    }
    RULE_CONDITION {
        bigint id PK
        bigint rule_id FK
        varchar field
        varchar operator
        varchar value
    }
    EXPORT_RECORD {
        bigint id PK
        bigint contract_id FK
        varchar format
        varchar file_path
        datetime created_at
    }
```

### 10.2 存储划分

| 数据 | 存储位置 | 理由 |
|---|---|---|
| 合同原件 | **MinIO** | 大文件不进 DB |
| 导出报告 | **MinIO** | 同上 |
| 段落树 + 锚点 | **MySQL** | 需查询与关联 |
| 全文 | **不单独存**（存于 CLAUSE） | 避免 LONGTEXT 拖慢查询 |
| 解析进度 | **Redis** | 高频更新 |
| OCR 结果缓存 | **Redis** | 按文件 hash 缓存，避免重复 6s/页 |

### 10.3 字符集与严格模式

> ⚠️ 本机 FlyEnv 实例的 `sql-mode = NO_ENGINE_SUBSTITUTION`（非严格模式），
> 合同金额溢出、超长文本会**静默截断**。容器实例已修正：

```ini
--character-set-server=utf8mb4
--collation-server=utf8mb4_0900_ai_ci
--sql-mode=STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION
--default-time-zone=+08:00
```

实测验证：生僻字（`龘靐齉爩`）、emoji（`📄✅🔍`）、特殊符号（`㎡ № ℡ ㈠`）
全部正确存储，4 字节字符 `24 bytes / 8 chars` 校验通过。

---

## 11. 核心业务流程

### 11.1 审批触发与自动审查

```mermaid
sequenceDiagram
    autonumber
    participant MOCK as mock 审批系统
    participant API as 后端接入模块
    participant PARSE as 解析引擎
    participant REVIEW as 审查引擎
    participant DB as MySQL / MinIO

    MOCK->>API: 推送审批事件（或 API 主动拉取）
    API->>MOCK: 下载合同附件
    MOCK-->>API: 附件字节流
    API->>API: 计算文件 hash 去重
    API->>DB: 创建任务（status=pending）
    API->>DB: 附件存 MinIO
    API->>DB: status=parsing

    API->>PARSE: 启动解析
    PARSE->>PARSE: 格式判定 → 分派路径
    PARSE-->>API: 上报进度（3/12 页）
    PARSE->>DB: 落库段落树 + 锚点
    API->>DB: status=reviewing

    API->>REVIEW: 规则 + LLM 审查
    REVIEW->>REVIEW: 双引擎 → 取高不取低
    REVIEW->>DB: 落库风险项 + 依据
    API->>DB: status=completed

    alt 解析异常
        API->>DB: status=blocked + 原因
        API->>MOCK: 推送告警
    end
```

### 11.2 法务人工复核与协同改写

```mermaid
sequenceDiagram
    autonumber
    participant LAW as 法务审查人
    participant FE as 工作台
    participant API as 后端
    participant DB as MySQL
    participant MOCK as mock 审批系统

    LAW->>FE: 打开待复核合同
    FE->>API: GET /contracts/{id}
    API-->>FE: 合同 + 风险卡片 + 锚点
    LAW->>FE: 点击风险卡片
    FE->>FE: 按锚点跳页 + 高亮
    LAW->>FE: 核对 / 调整修改建议
    LAW->>FE: 录入补充法务意见
    LAW->>FE: 点击「写回审批意见」
    FE->>API: POST /contracts/{id}/writeback
    API->>DB: writeback_status=writing
    API->>MOCK: POST 评论接口
    alt 成功
        MOCK-->>API: 200
        API->>DB: writeback_status=success
    else 失败
        MOCK-->>API: 异常 / 超时
        API->>DB: writeback_status=failed
    end
    FE->>API: 轮询回写状态
    API-->>FE: 最终状态
    LAW->>FE: 下载整改对比报告
```

### 11.3 异常任务阻塞与重试

```mermaid
sequenceDiagram
    autonumber
    participant PARSE as 解析引擎
    participant API as 后端
    participant DB as MySQL
    participant ADMIN as 系统管理员

    PARSE->>PARSE: 文档加密 / 空白 / 严重模糊
    PARSE-->>API: 抛解析异常
    API->>DB: status=blocked + blocked_reason
    API->>ADMIN: 推送告警
    ADMIN->>API: 查看阻塞原因
    ADMIN->>API: 修正文件 / 调整参数
    ADMIN->>API: POST /tasks/{id}/retry
    API->>DB: status=parsing
    API->>PARSE: 重新解析
```

### 11.4 前端交互（工作台）

```mermaid
flowchart LR
    subgraph LEFT["左侧 · 合同正文"]
        L1["PDF.js 渲染"]
        L2["文本层高亮<br/>char_range"]
        L3["覆盖层画框<br/>bbox"]
        L4["分页滚动"]
    end

    subgraph RIGHT["右侧 · 审查结果"]
        R1["风险卡片<br/>高/中/低分级"]
        R2["违规原因 + 法律依据"]
        R3["AI 推荐修改条款"]
        R4["采纳 / 编辑"]
    end

    subgraph BOTTOM["底部 · 协同回写"]
        B1["风险评估摘要"]
        B2["法务批注输入"]
        B3["一键回写"]
        B4["导出报告"]
    end

    R1 -->|"点击"| L2
    L2 -->|"点击高亮"| R1
    R4 --> B1
    B2 --> B3
    B3 --> B4
```

**双向锚定的实现要点**

- 风险卡片 → 正文：用 `page` 跳页，用 `bbox` 滚动到可视区并高亮
- 正文 → 风险卡片：文本层 span 携带 `risk_item_id`，点击反向定位卡片
- 扫描件无文本层：只用 `bbox` 覆盖层，点击命中检测用坐标范围

---

## 12. 后端模块划分

```mermaid
flowchart TB
    subgraph API["api/ · 路由层"]
        A1["contracts"]
        A2["tasks"]
        A3["risks"]
        A4["rules"]
        A5["writeback"]
        A6["reports"]
    end

    subgraph SVC["services/ · 业务层"]
        subgraph PARSING["parsing/"]
            P1["dispatcher<br/>格式判定"]
            P2["pdf_extractor"]
            P3["docx_converter"]
            P4["ocr_engine"]
            P5["anchor_builder"]
        end
        subgraph REVIEW["review/"]
            R1["rule_engine"]
            R2["llm_reviewer"]
            R3["merger<br/>取高不取低"]
            R4["global_checker"]
        end
        subgraph LLM["llm/"]
            L1["LLMProvider"]
            L2["OpenAICompat"]
            L3["Mock"]
        end
        S1["writeback_service"]
        S2["report_service"]
    end

    subgraph DATA["core/ · 基础设施"]
        D1["config"]
        D2["database"]
        D3["redis_client"]
        D4["minio_client"]
    end

    API --> SVC
    SVC --> DATA
```

**模块职责**

| 模块 | 职责 | 关键点 |
|---|---|---|
| `parsing/dispatcher` | 格式判定与路径分派 | 单一入口，避免调用方关心格式 |
| `parsing/docx_converter` | DOCX → PDF | 策略模式，三级降级 |
| `parsing/ocr_engine` | 扫描件 OCR | 结果缓存（按文件 hash） |
| `parsing/anchor_builder` | 构建统一锚点 | 坐标映射的唯一实现点 |
| `review/rule_engine` | 确定性规则匹配 | 关键词匹配放应用层，**不下推给 DB** |
| `review/llm_reviewer` | LLM 语义研判 | 带防幻觉闸门 |
| `review/merger` | 双引擎合并 | 取高不取低，双来源留痕 |
| `review/global_checker` | 必备条款全局校验 | 弥补分块审查的盲区 |
| `llm/` | Provider 抽象 | 默认 Mock |

---

## 13. 异步任务设计

### 13.1 方案选择

**DB 状态机 + 进程内后台任务**（FastAPI `BackgroundTasks`）。

| 对比项 | 本方案 | Celery + Redis |
|---|---|---|
| 并发能力 | 弱（演示并发量为 1） | 强 |
| 依赖 | 无额外中间件 | 需 Celery + Redis |
| 演示失败点 | 少 | 多一个必须启动的服务 |
| 重启恢复 | 靠 `parsing` 状态恢复 | 内建 |

**结论**：阶段一用本方案。任务表设计成**可迁移的**，阶段二若需批量审查再迁 Celery。

### 13.2 任务执行流程

```mermaid
flowchart TD
    T["任务创建 pending"] --> W["后台任务启动"]
    W --> U["status=parsing"]
    U --> P["逐页解析<br/>每页上报进度到 Redis"]
    P --> CHK{"全部完成?"}
    CHK -->|否| P
    CHK -->|是| R["status=reviewing"]
    R --> REV["规则 + LLM 审查"]
    REV --> D["status=completed"]
    P -.->|异常| B["status=blocked<br/>+ blocked_reason"]
    B -.->|人工重试| U
```

### 13.3 幂等性

- 任务重试时**先清理旧的段落树与锚点**，避免重复数据
- 回写用 `writeback_log` 表记录，**同一合同重复触发时先查是否已 success**
- 文件去重用 `file_hash`，相同文件不重复解析（直接命中 OCR 缓存）

---

## 14. 前端架构

### 14.1 页面结构

```
┌───────────────────────────────────────────────────────────────┐
│  合同审查大盘页                                                │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ [风险筛选 ▾] [状态筛选 ▾]           [+ 上传合同]        │  │
│  ├─────────────────────────────────────────────────────────┤  │
│  │ 合同名称    申请人  业务类型  金额    状态   风险  时间  │  │
│  │ 设备采购…   张三    采购      229万   完成   高    09-26 │  │
│  │ 系统销售…   李四    销售      580万   审查中 中    09-26 │  │
│  │ 劳动合同…   王五    劳动      —       阻塞    —    09-26 │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│  智能审查工作台                                                │
│  ┌──────────────────────────┬──────────────────────────────┐  │
│  │ 合同正文（PDF.js）        │ 审查结果与建议                │  │
│  │ ┌──────────────────────┐ │ ┌──────────────────────────┐ │  │
│  │ │ 第 1 页 / 共 3 页     │ │ │ 🔴 高风险  知识产权归属   │ │  │
│  │ │                      │ │ │ 原因：…  依据：《民法典》…│ │  │
│  │ │  第二条 知识产权     │ │ │ 建议：双方共有 / 甲方所有 │ │  │
│  │ │  ▓▓▓▓▓▓▓▓▓▓▓▓  ←高亮 │ │ │ [采纳] [编辑] [复制]      │ │  │
│  │ │                      │ │ ├──────────────────────────┤ │  │
│  │ │  第四条 付款方式     │ │ │ 🟠 中风险  违约金比例     │ │  │
│  │ │  ▓▓▓▓▓▓▓▓▓▓▓▓  ←高亮 │ │ │ …                        │ │  │
│  │ │                      │ │ └──────────────────────────┘ │  │
│  │ │ [◀ 上一页] [下一页 ▶] │ │                              │  │
│  │ └──────────────────────┘ │ ┌──────────────────────────┐ │  │
│  │                          │ │ 综合风险：🔴 高            │ │  │
│  │                          │ │ 结论：建议整改后重审        │ │  │
│  │                          │ │ ┌──────────────────────┐ │ │  │
│  │                          │ │ │ 法务批注…            │ │ │  │
│  │                          │ │ └──────────────────────┘ │ │  │
│  │                          │ │ [写回审批意见] [导出报告] │ │  │
│  │                          │ └──────────────────────────┘ │  │
│  └──────────────────────────┴──────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│  合规规则与模板页                                              │
│  ┌──────────────┬──────────────────────────────────────────┐  │
│  │ 合同类型      │ 规则项列表                                │  │
│  │ ▸ 采购合同    │ ┌──────────────────────────────────────┐ │  │
│  │ ▸ 销售合同    │ │ 违约金比例上限  [中风险]  [启用 ✓]    │ │  │
│  │ ▸ 服务合同    │ │   条件: 违约金比例 > 20%              │ │  │
│  │ ▸ 劳动合同    │ │   示范条款: 违约金不超过总额 20%      │ │  │
│  │              │ ├──────────────────────────────────────┤ │  │
│  │              │ │ 管辖地约定      [高风险]  [启用 ✓]    │ │  │
│  │              │ │   条件: 命中违规清单                  │ │  │
│  │              │ └──────────────────────────────────────┘ │  │
│  └──────────────┴──────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

**阶段一实现范围**：大盘页 + 工作台（上面前两张图）。
**合规规则与模板页属阶段二**（§18.1 范围表）——阶段一规则库只读，
通过 `GET /api/rules/*` 暴露数据，尚无编辑界面。

### 14.2 技术选型

| 项 | 选择 | 理由 |
|---|---|---|
| 框架 | React 18 + TypeScript | 类型安全 |
| 构建 | Vite | 开发期 HMR 快 |
| UI 库 | Ant Design | 表格/表单/树控件直接吃下大盘页与规则页 |
| 文档渲染 | **PDF.js** | 统一渲染，文本层 + 覆盖层 |
| 状态管理 | **React Query**（`@tanstack/react-query`） | 以服务端状态为主；审查进度轮询用 `refetchInterval` 即可，不必引全局 store |
| 请求 | axios / fetch | — |

### 14.3 为什么统一用 PDF.js

实测证明四条输入路径都能收敛到 PDF 坐标系（见 §6）。
因此前端**只需要一套渲染路径**：

- 文本层高亮（`char_range`）与覆盖层画框（`bbox`）**在同一坐标空间**
- 不需要为 DOCX 单独写 HTML 渲染，也不需要为扫描件单独写图片渲染

**代价**：DOCX 展示的是转换后的 PDF，不是原件排版；用户下载原件时页码可能对不上。
报告中将明确标注"页码基于 WPS 排版"。

---

## 15. 安全与合规

### 15.1 凭据管理

- 所有凭据走 `.env`，**不进 Git**（`.gitignore`）
- `compose.yml` 用 `${VAR}` 引用，不硬编码
- 提供 `.env.example` 作为模板

### 15.2 数据隔离

| 措施 | 说明 |
|---|---|
| 独立数据库实例 | 容器 MySQL，**不碰宿主 FlyEnv 的 20 个库** |
| 最小权限用户 | `GRANT ALL ON contract_review.*`，仅此一库 |
| 端口错开 | 13306/16379/19000，避免误连 |
| 文件 hash 去重 | 防止重复解析，也便于审计 |

### 15.3 LLM 数据外发

> ⚠️ 合同文本属于**敏感商业数据**，发送到第三方 LLM API 存在合规风险。

- 默认 `MockProvider`，**不外发任何数据**
- 启用真实 Provider 时，在 UI 明确提示"合同内容将发送至外部 LLM 服务"
- 演示用虚构合同，不含真实商业信息

### 15.4 示例合同命名

虚构合同中的公司名**不使用任何真实企业名**，一律用「示例科技有限公司」这类
明显虚构的命名，并在合同头部标注「本文档为演示用虚构样本」，
避免生成物被误当成真实合同或侵犯名称权。

---

## 16. 测试策略

### 16.1 测试范围

| 类型 | 内容 | 是否必需 |
|---|---|---|
| 核心业务逻辑 | 状态机流转、规则引擎判定、取高合并 | ✅ 必需 |
| 边界/错误路径 | 加密文档、空白文档、超长文本、OCR 失败 | ✅ 必需 |
| 外部集成 | LLM Provider、mock 审批、WPS COM（最小 Mock） | ✅ 必需 |
| 锚点对齐 | 引用定位的三级降级 | ✅ 必需 |
| 回归断言集 | 每个示例合同的期望风险点 | ✅ 必需 |
| 覆盖率导向的测试 | — | ❌ 不做 |
| 实现细节测试 | 具体颜色值、类名 | ❌ 不做 |

### 16.2 回归断言集设计

为每份示例合同人工标注**期望风险点**，落在 `samples/expected/*.expected.json`：

```json
{
  "contract": "设备采购合同-高风险样本.docx",
  "sample_dir": "purchase",
  "business_type": "purchase",
  "expected_overall": "high",
  "expected_conclusion": "reject",
  "expected_risks": [
    {
      "title": "知识产权归属供应商",
      "match_keywords": ["知识产权"],
      "risk_level": "high",
      "must_anchor": true,
      "expected_anchor_page": 1,
      "expected_anchor_level": "exact",
      "anchor_quote": "第八条知识产权本项目产生的知识产权归供应商所有。"
    }
  ]
}
```

**为什么用 `match_keywords` 而不是 `title` 做匹配**：规则引擎用 `rule.name`，
LLM 用自己的措辞，两者几乎不会一致（"违约责任无上限" vs "赔偿责任无上限"，
实测标题相似度仅 0.42~0.84）。`title` 只用于报告展示，
匹配判据是"实际风险项的 `title` / `reason` / `legal_basis` 拼接文本包含任一关键词"。

**断言维度**

| 维度 | 判据 |
|---|---|
| 风险点是否被识别（召回） | `match_keywords` 命中任一风险项 |
| 风险等级是否正确 | `risk_level` 相等 |
| 锚点是否定位到正确页码 | `must_anchor` 时 `page_no == expected_anchor_page` |
| 锚点级别是否符合预期 | `anchor_level == expected_anchor_level` |
| **锚点是否真的框住引用原文** | 用渲染用 PDF + PDF.js 实测 bbox 覆盖情况（`--verify-anchors`） |
| 综合风险等级 / 结论 | 与 `expected_overall` / `expected_conclusion` 相等 |

**锚点级别由规则类型决定**（实测规律）：

- KEYWORD / REGEX / BLACKLIST 类规则能提取命中子串 → `exact`，覆盖整段引用
- PRESENCE 类规则（"必须包含某模式"）没有命中子串 → `paragraph`，覆盖**整条条款**
  （按页切分，跨页条款产出多个锚点；锚定对象是整条而非子串，因此不带字符区间）

**最后一项为什么要走 PDF.js**：后端锚点由 PyMuPDF 产生，
用 PyMuPDF 自校验是同源验证，测不出"前端坐标换算公式错"。
实测该项曾抓到真实缺陷——用 `viewport.convertToViewportRectangle()`
（假定左下原点、会翻转 y）时 6 个锚点只有 1 个落在引用文字上；
改用左上原点直乘 scale 后 6/6 命中。

**执行方式**

```bash
python scripts/run_tests.py                    # 只校验风险识别与等级
python scripts/run_tests.py --verify-anchors   # 附加锚点坐标校验（需 node）
```

退出码 0 = 全部通过，可直接用于 CI 或提交前检查。

### 16.3 不做量化指标的理由

PRD 未提供真实工商数据源与真实审批系统，生产化的两个前置条件缺失。
搭带标注的评测集投入产出不划算；**断言集足以防止回归**。

---

## 17. 项目结构

```
contract-approval-and-review-system/
├── AGENTS.md                      # 项目规范
├── PRD.md                         # 需求文档
├── README.md                      # 快速开始
├── compose.yml                    # 基础设施编排（mysql/redis/minio）
├── .env.example                   # 环境变量模板
├── .env                           # 实际凭据（.gitignore）
├── .gitignore
│
├── docs/
│   ├── architecture.md            # 本文档
│   ├── data-model.md              # 数据对象设计
│   ├── data-layer-guide.md        # 数据层使用说明
│   ├── review-engine.md           # 审查引擎说明
│   ├── api-guide.md               # 后端 API 手册
│   └── mock-approval.md           # mock 审批系统说明
│
├── backend/                       # FastAPI · 原生运行
│   ├── app/
│   │   ├── main.py
│   │   ├── api/                   # 路由
│   │   ├── core/                  # config / database / redis / minio
│   │   ├── models/                # SQLAlchemy 模型
│   │   ├── schemas/               # Pydantic 模型
│   │   ├── services/
│   │   │   ├── parsing/           # dispatcher / pdf / docx / ocr / anchor
│   │   │   ├── review/            # clause_splitter / rule_engine / llm_reviewer
│   │   │   │                      #   / merger / global_checker
│   │   │   ├── llm/               # LLMProvider / OpenAICompat / Mock
│   │   │   ├── approval/          # ApprovalSystemAdapter 抽象 + HTTP 实现
│   │   │   ├── report_service.py  # 报告渲染与导出
│   │   │   └── writeback_service.py  # 回写审批系统（幂等）
│   │   └── workers/               # state_machine / pipeline
│   ├── tests/                     # test_models / test_review_engine / test_api
│   ├── alembic/                   # 数据库迁移
│   └── pyproject.toml
│
├── frontend/                      # React + Vite · 原生运行
│   ├── src/
│   │   ├── pages/                 # 大盘页 / 工作台
│   │   ├── components/            # PdfViewer / RiskCard / WritebackBar
│   │   ├── lib/                   # anchorCoords（锚点坐标换算，前后端契约唯一实现）
│   │   ├── api/                   # 接口封装
│   │   └── types/                 # 与后端 schemas 对应的类型
│   ├── scripts/                   # verify_anchors.mjs（锚点坐标校验）
│   └── package.json
│
├── mock-approval/                 # mock 审批服务 · 独立
│   ├── app/                       # FastAPI 应用（M1~M4）
│   ├── data/                      # JSON 存储（待办 / 评论 / 附件）
│   └── seed.py                    # 种子数据脚本
│
├── samples/                       # 示例合同集
│   ├── purchase/                  # 采购合同（含高风险用例）
│   └── expected/                  # 人工标注的期望风险点（断言集）
│
└── scripts/
    ├── start_all.py               # 一键启动（基础设施 → 数据 → 三个服务）
    ├── stop_all.py                # 一键停止（含归属校验，不误杀同端口进程）
    ├── check_env.py               # 环境自检
    ├── check_consistency.py       # 数据一致性检查 C1~C8
    ├── seed_data.py               # 规则库 / 示范条款 / 黑名单种子
    └── run_tests.py               # 回归断言集执行器
```

**目录划分原则**

- `mock-approval/` **独立于 `backend/`**：它扮演"外部系统"，不应与后端共享代码，
  只通过 HTTP 契约耦合。将来换真实审批系统只改适配层。
- `samples/expected/` 与 `samples/` 并列：断言集是**一等公民**，不是附属物。
- `frontend/src/lib/anchorCoords.ts` 是锚点坐标换算的**唯一实现**：
  前端组件与 `frontend/scripts/verify_anchors.mjs` 共用它，
  避免两处各写一份公式导致"校验通过但页面画错"。

---

## 18. 实施计划

### 18.1 阶段一：MVP（端到端单合同闭环）

```mermaid
gantt
    title 阶段一实施计划
    dateFormat YYYY-MM-DD
    section 地基
    项目骨架与配置层        :a1, 2026-09-27, 1d
    数据模型与迁移          :a2, after a1, 1d
    section 解析
    PyMuPDF 提取器          :b1, after a2, 1d
    DOCX 转换器（WPS COM）  :b2, after b1, 1d
    锚点构建与对齐          :b3, after b2, 2d
    section 审查
    规则引擎                :c1, after b3, 1d
    LLM Provider 抽象       :c2, after c1, 1d
    双引擎合并              :c3, after c2, 1d
    section 服务
    mock 审批服务           :d1, after a2, 1d
    后端 API 路由           :d2, after c3, 2d
    section 前端
    大盘页                  :e1, after d2, 1d
    工作台（双栏+高亮）     :e2, after e1, 3d
    section 收尾
    示例合同 + 断言集       :f1, after e2, 1d
    端到端联调              :f2, after f1, 1d
```

**阶段一范围**

| 包含 | 不包含（推阶段二） |
|---|---|
| 后端：数据模型 + 状态机 + 解析 + 规则 + LLM + 报告（Markdown） | 扫描件 OCR 链路 |
| 前端：大盘页 + 工作台 | 规则配置页 |
| mock 审批服务 | PDF 报告精排 |
| 1 个示例合同端到端（设备采购，DOCX） | 其余示例合同（销售/劳动） |
| 回归断言集（1 份合同） | blocked 重试的 UI |

**为什么先做设备采购合同**：PRD 2.4.9 只要求"至少一个场景"，而设备采购合同
同时覆盖"知识产权归属"与"付款无验收"两个高风险点，演示效果最完整。

### 18.2 阶段二：完整交付

- 规则配置页（规则树 + 条件编辑 + 示范条款库）
- 扫描件 OCR 链路接入 + 缓存
- 其余示例合同（销售 / 劳动）+ 完整断言集
- PDF 报告精排
- blocked 重试 UI
- 批量审查（届时评估迁 Celery）

---

## 19. 风险登记册

| # | 风险 | 影响 | 缓解措施 | 状态 |
|---|---|---|---|---|
| R1 | WPS COM 依赖交互式桌面，不能跑成服务 | 后端若服务化则 DOCX 分页失效 | 策略模式三级降级；文档明确限制 | 已缓解 |
| R2 | WPS 与 Word 分页不一致 | 用户用 Word 打开时页码对不上 | 报告标注"页码基于 WPS 排版" | 接受 |
| R3 | OCR 丢失表格结构 | 扫描件元数据质量下降 | 打置信度标记，UI 提示人工核对 | 接受 |
| R4 | LLM 幻觉编造原文引用 | 高亮定位失败，验收标准不达标 | 三级对齐 + `unanchored` 显式标记 | 已缓解 |
| R5 | LLM 编造法律依据 | 报告可信度受损 | 限定知识库选取或标注"待人工复核" | 已缓解 |
| R6 | 无 LLM API Key | 无法演示真实效果 | `MockProvider` 兜底，默认 Mock | 已缓解 |
| R7 | CPU OCR 分钟级耗时 | 演示等待过长 | 示例合同用 DOCX 快速路径；OCR 结果缓存 | 已缓解 |
| R8 | 容器内存上限 7.87 GB | 大文档并发解析可能 OOM | 演示串行执行；阶段二评估并发上限 | 接受 |
| R9 | 风险阈值是拟值非法律意见 | 误判 | 文档显式声明；阈值可配置 | 接受 |
| R10 | 9p 挂载比 ext4 慢 1.8~2.2 倍 | 数据库性能下降 | 数据卷用 named volume（ext4），非 bind mount | 已缓解 |

---

## 附录 A：技术验证实测记录

> 所有数据来自本机实测，脚本见 `docs/probe/`。

### A.1 解析性能

| 方案 | 耗时 | 内存 | 备注 |
|---|---|---|---|
| PyMuPDF `get_text("dict")` | 9.26 ms / 3 页 | — | 支持 span bbox |
| PyMuPDF `get_text("rawdict")` | 10.30 ms / 3 页 | — | 支持**字符级** bbox |
| python-docx 全量解析 | 51.15 ms | — | **无分页信息** |
| WPS COM 导出 PDF | 1.41 s | — | 启动 0.82 + 加载 0.34 + 导出 0.25 |
| LibreOffice 导出 PDF | 1.55 s | — | 分页与 WPS **不一致**（2 vs 3 页） |
| RapidOCR 单页 | 6.08 s | ~150 MB | 质量准确 |
| PaddleOCR 单页（关 mkldnn） | 69.05 s | ~690 MB | 慢 11 倍 |
| PaddleOCR（开 mkldnn） | — | — | **崩溃**（oneDNN bug） |

### A.2 WPS COM 可靠性

```
连续 5 次转换：5/5 成功
  启动 0.82s  加载 0.34s  导出 0.25s
  页数 = 3（稳定）
  产物字节 = 272060 × 5（完全一致，确定性）
  进程泄漏 = 无

多线程（FastAPI 场景）：
  + CoInitialize：    3 线程并发全成功，3.36s
  无 CoInitialize：   全部失败（尚未调用 CoInitialize）
  → 结论：必须在线程内调 CoInitialize
```

### A.3 坐标映射验证

```
OCR 像素 × (72/200) = PDF point
实测误差 0.2 ~ 5.1 pt（OCR 框 padding 正常范围）
```

### A.4 基础设施

| 项 | 实测 |
|---|---|
| 启动耗时 | 19 秒达 3/3 healthy |
| MySQL 版本 | 8.4.11 |
| 字符集 | utf8mb4 / utf8mb4_0900_ai_ci |
| 严格模式 | STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION |
| 时区 | +08:00 |
| 中文/生僻字/emoji | ✅ 全部正确存储 |
| Redis 版本 | 8.10.2 |
| MinIO | healthy，mc 可用 |

### A.5 SQLite on 9p 挂载（已否决方案）

> 记录此实验是因为**我最初的测试配置有误**（`busy_timeout=0`），
> 得出过"9p 丢数据"的错误结论。修正后的公平对比：

| 测试 | 命名卷 ext4 | E 盘 bind mount 9p |
|---|---|---|
| 并发写 4×250（期望 1000 行） | 1000 ✅ | 1000 ✅ |
| 锁错误数 | 0 | 0 |
| `integrity_check` | ok | ok |
| 批量写 5000 行 | 11.6 ms | 25.1 ms（**2.2× 慢**） |
| 200 次独立事务 | 318.7 ms | 584.1 ms（**1.8× 慢**） |

**结论**：9p 无正确性问题，仅慢 1.8~2.2 倍。WAL 崩溃恢复测试通过
（`integrity_check=ok`，20000 行全恢复）。
最终仍选 named volume（ext4）以规避性能损失。

---

## 附录 B：术语表

| 术语 | 含义 |
|---|---|
| **锚点（Anchor）** | 指向原文位置的数据结构，本项目统一为 PDF 坐标系 |
| **原生文本层（native_text）** | PDF/DOCX 转换后自带的可选中文字层 |
| **char_range** | 文本层中的字符区间，扫描件为 null |
| **bbox** | 边界框 `[x0, y0, x1, y1]`，PDF point 坐标 |
| **取高不取低** | 规则与 LLM 定级冲突时取更严重的一方 |
| **阻塞（blocked）** | 文档加密/空白/严重模糊导致的解析中断状态 |
| **断言集** | 人工标注的期望风险点，用于回归测试 |
| **Passthrough 降级** | DOCX 无法转 PDF 时，退化为无分页的段落级锚点 |

---

## 附录 C：待办与开放问题

| # | 事项 | 状态 |
|---|---|---|
| 1 | LLM `base_url` / `api_key` / `model` | 待提供（不阻塞，Mock 先跑通） |
| 2 | 凭据从 `compose.yml` 迁至 `.env` | 待执行 |
| 3 | 清理验证时产生的 `charset_test` 表 | 待执行 |
| 4 | WPS 三个计划任务是否禁用 | 待决策（属改环境操作） |
| 5 | 风险阈值表法务校准 | 阶段二 |
| 6 | 示例合同正式内容（按 PRD 2.4.9 重写） | 阶段一 |
