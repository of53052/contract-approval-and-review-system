# 审查引擎操作手册

> 面向开发者的日常操作指引。设计依据：[`docs/architecture.md`](architecture.md) §5~§9、§13。
> 本文件讲**怎么用、怎么验、有哪些坑**，设计理由看架构文档。

---

## 1. 模块地图

```
backend/app/services/
├── parsing/                 解析层：文件 → 统一段落树 + 字符级坐标
│   ├── types.py             CharBox / PageText / ParsedBlock / DocumentText
│   ├── text_normalize.py    全角转半角、标点统一、similarity
│   ├── pdf_extractor.py     PyMuPDF rawdict 提取 + 字体度量修复
│   ├── docx_converter.py    WPS COM / LibreOffice / Passthrough 三级降级
│   ├── ocr_engine.py        RapidOCR（阶段一不启用）
│   ├── anchor_builder.py    引用 → 坐标，四级降级（唯一坐标映射点）
│   └── dispatcher.py        格式判定 + 路径分派，唯一入口 parse_document()
├── review/                  审查层：段落树 → 风险清单
│   ├── clause_splitter.py   条款切分 + 元数据提取
│   ├── rule_engine.py       确定性规则（5 种类型）
│   ├── llm_reviewer.py      分批送审 + 字段级容错
│   ├── merger.py            取高不取低 + 双来源留痕
│   └── global_checker.py    必备条款全局校验 + 防幻觉闸门
└── llm/                     LLM 接入层
    ├── base.py              LLMProvider Protocol / ChatMessage / ChatResult
    ├── json_utils.py        JSON 四策略提取
    ├── openai_compat.py     真实端点（任意 OpenAI 兼容 base_url）
    ├── mock_provider.py     7 组预置 fixture，无 Key 时兜底
    └── __init__.py          get_provider() 工厂

backend/app/workers/
├── state_machine.py         任务状态机 + 乐观锁 + 重试清理
└── pipeline.py              完整流水线编排（唯一入口 Pipeline.run()）
```

**调用链**：`Pipeline.run()` → `parse_document()` → `split_document()` → `RuleEngine` + `LlmReviewer` → `Merger` → `HallucinationGate` → 落库。

---

## 2. 快速验证

```powershell
$py = "backend\.venv\Scripts\python.exe"

# 单元 + 集成测试（含 WPS COM 端到端，34 项）
& $py -m pytest backend\tests -q

# 只跑审查引擎
& $py -m pytest backend\tests\test_review_engine.py -q

# 数据一致性（C1~C8，应在跑完流水线后全绿）
& $py scripts\check_consistency.py
```

---

## 3. 流水线执行顺序

```
pending
  │
  ├─ transition → parsing
  │    ├─ parse_document(path)        # 格式判定 → 转换/提取 → DocumentText
  │    ├─ _save_parse_result()        # 写 parse_result（attempt 递增）
  │    ├─ _upload_converted_pdf()     # DOCX 产物真实上传 MinIO，再登记 key
  │    └─ record_progress() + Redis 进度
  │
  ├─ transition → reviewing
  │    ├─ split_document()            # 条款 + 元数据
  │    ├─ _backfill_contract_fields() # 回填 contract.amount / contract_no / 相对方
  │    ├─ RuleEngine.run()            # 确定性规则
  │    ├─ LlmReviewer.review()        # LLM 研判（分批，失败不中断）
  │    ├─ Merger.merge()              # 取高不取低
  │    ├─ _append_global_missing()    # 必备条款底线校验
  │    ├─ HallucinationGate.apply()   # 锚定 + 法条核验
  │    └─ _save_risks()               # risk_item + anchor + risk_evidence
  │
  └─ transition → completed（写 overall_risk / conclusion / 冗余计数 / summary）
```

**受阻路径**：`parse_document` 抛 `ParseBlocked` → `block()` → `blocked`，携带 `blocked_reason`（加密 / 空白 / 转换器不可用 / 超时 / 其他）。

---

## 4. 各阶段关键约定

### 4.1 解析：统一 PDF 坐标系

四条输入路径（DOCX / 文本 PDF / 扫描 PDF / 图片）全部收敛到 PDF point 坐标，前端只需一套 PDF.js 渲染。

| 路径 | 实现 | 备注 |
|---|---|---|
| DOCX | WPS COM 导出 PDF → PyMuPDF 提取 | **分页来自 WPS 排版**，报告须标注 |
| 文本 PDF | PyMuPDF `rawdict` 字符级 bbox | 最快路径 |
| 扫描件 / 图片 | PyMuPDF 渲染 + RapidOCR | 阶段一不启用，`allow_ocr=False` 时直接 blocked |

**降级链**：`wps_com` → `libreoffice` → `passthrough`。实际链落库到 `parse_result.converter_fallback_chain`，用于解释报告里的页码语义。

### 4.2 锚点：四级降级（`anchor_builder.locate`）

| 顺序 | 方法 | 触发条件 | 产出 level |
|---|---|---|---|
| ①-a | `_match_raw` | 原文逐字包含 | `exact` |
| ①-b | `_match_compact` | 去空白后包含（跨行引用） | `exact` |
| ①-c | `_match_fragment` | 按标点切片段，锚定最长可定位片段 | 命中片段本身的 level，`partial=True` |
| ② | `_match_fuzzy` | 归一化 + 等长窗口相似度 ≥ 0.72 | `fuzzy` |
| ③ | `locate_block` | 知道条款位置但引用定位不到 | `paragraph` |

**全部失败** → `level=none`，上层把 `risk_item.unanchored` 置 1，**不写 anchor 行、不伪造位置**。

### 4.3 合并：取高不取低

配对判据（`Merger._find_partner`，按优先级）：
1. 同一 `clause_index`
2. 标题归一化相似度 ≥ 0.55
3. 引用归一化相似度 ≥ 0.55

配对成功 → `merged_by=both`，等级取两者较高，两条依据都写入 `risk_evidence`。

### 4.4 闸门：防幻觉三条硬规则

1. JSON 解析失败必须重试或报错（在 provider 层）
2. 引用无法锚定必须标记（`unanchored=1`），不得丢弃或伪造
3. 法条无法在 `standard_clause.source` 中匹配 → `need_review=1`

---

## 5. 实测数据（设备采购合同-高风险样本.docx）

```
解析:      WPS COM 转 PDF，2 页，1.9 s
条款切分:  10 条
元数据:    8 项（甲乙方名称/信用代码、金额、币种、合同编号、履行期限）
规则命中:  6 项
LLM 研判:  MockProvider（真实端点另测通过）
合并结果:  规则独有 4 / LLM 独有 0 / 双来源 2 / 取高升级 0
整体风险:  high  结论: reject
```

| seq | 等级 | 来源 | 锚定 | 风险项 |
|---|---|---|---|---|
| 0 | high | rule | paragraph | 未设置付款前置验收 |
| 1 | high | both | exact | 违约责任无上限 |
| 2 | high | rule | exact | 管辖地约定违规 |
| 3 | high | both | exact | 知识产权全部转让对方 |
| 4 | medium | rule | paragraph | 保密义务无期限 |
| 5 | medium | rule | paragraph | 不可抗力无通知时效 |

> `paragraph` 级的项是 `GLOBAL` 类问题（必备条款缺失 / 无固定位置），按设计只定位到条款段。

---

## 6. 已修复的真实缺陷（回归用例已覆盖）

| # | 缺陷 | 现象 | 修法 |
|---|---|---|---|
| 1 | WPS 表格字体度量畸形 | Cambria 字体 `ascender=3.116`，字符 bbox 高是字号 5.6 倍 | `_needs_metric_repair()` 按"bbox 高/字号 > 1.6"判定，用标称度量重算纵向 |
| 2 | 模糊匹配窗口稀释 | `SequenceMatcher.ratio=2M/T`，窗口长 4 倍时完全匹配只得 0.39 | 窗口长度固定等于目标长度，仅在对齐偏移 ±8 内滑动 |
| 3 | LLM 省略中段无法定位 | 省略版引用相似度仅 0.44，模糊匹配必漏 | `_match_fragment` 按标点切片段，锚定最长可定位片段并标 `partial` |
| 4 | 金额正则贪婪回溯 | `[^）)]*` 吃到行尾，"2,299,000.00" 捕获成 "0" | 改有界量词 `[^）)]{0,20}` |
| 5 | `clause_index` 下标语义错位 | 索引进 `doc.blocks`（10 条款 vs 25 块）导致锚定到无关段落 | `HallucinationGate` 强制传 `clauses`，索引进条款列表 |
| 6 | `within_clause_type` 表达力缺口 | "付款未以验收为前置"用存在性判定会漏报 | 新增 `within_clause_type`：在指定类型条款内检查必须模式 |
| 7 | 转换产物只登记不上传 | `pdf_object_key` 指向 MinIO 里不存在的对象 | 先 `put_object` 再写 key，失败则不登记 |
| 8 | `contract` 冗余字段从不回填 | 大盘页要显示的金额/编号永远为 NULL | 新增 `_backfill_contract_fields`，不覆盖已有值 |

---

## 7. 已知限制与坑

### 7.1 WPS COM 噪声（无害）

反复调用 `WpsComConverter.to_pdf` 时，pywin32 在释放已 `Quit()` 的 COM 代理时会在 stderr 打印：

```
Windows fatal exception: code 0x800706be
```

- **不影响结果**：转换成功、进程退出码正常、`wps.exe` 数秒内自行退出
- 只在 `faulthandler` 启用时可见（pytest 默认启用），纯属噪声
- 已尝试多种释放顺序（先 `del`、先 `CoUninitialize`、加 `gc.collect()`），**均无法消除**

### 7.2 WPS COM 的硬约束

- 依赖**交互式桌面会话**，后端若跑成 Windows 服务会失败（架构 R1）
- 多线程下必须本线程 `CoInitialize()`；本模块用全局锁串行化
- 分页结果与 Word/LibreOffice **可能不同**，报告须标注"页码基于 WPS 排版"

### 7.3 测试里的 ORM 缓存坑

测试用原始 SQL 改 `review_task.status` 后，**必须 `db.expire_all()`**：`SessionLocal` 配了 `expire_on_commit=False`，原始 SQL 绕过身份映射，不 expire 会读到过期对象，导致"非法流转"之类的假失败。

### 7.4 阶段一明确不做

扫描件 OCR 链路（代码已就绪，`allow_ocr=False`）、规则配置页、PDF 报告精排、合同版本管理、blocked 重试 UI。

---

## 8. LLM Provider

```python
from app.services.llm import get_provider

provider = get_provider()          # 自动判定：配置完整 → 真实端点，否则 Mock
provider = get_provider(force_mock=True)   # 演示/测试强制 Mock
```

回落规则（`settings.use_real_llm`）：`LLM_PROVIDER=openai_compat` **且** `base_url`/`api_key`/`model` 三项齐备才走真实端点，否则回落 Mock。

配置全在项目根 `.env`：

```ini
LLM_PROVIDER=openai_compat
LLM_BASE_URL=https://your-endpoint/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=your-model
```

**JSON 四策略提取**（`json_utils.extract_json`）：直接解析 → 去 markdown 围栏 → 括号平衡扫描 → 首个 `{...}` 子串。任一成功即返回，全失败抛 `LlmJsonError`。
