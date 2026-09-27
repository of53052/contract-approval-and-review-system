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
│   ├── ocr_engine.py        RapidOCR + 行→块组装（批次 8 起启用）
│   ├── anchor_builder.py    引用 → 坐标，四级降级（唯一坐标映射点）
│   └── dispatcher.py        格式判定 + 路径分派，唯一入口 parse_document()
├── review/                  审查层：段落树 → 风险清单
│   ├── clause_splitter.py   条款切分 + 元数据提取
│   ├── rule_engine.py       确定性规则（5 种类型 + 4 种 metric）
│   ├── rule_config.py       规则写入校验（拒"配了等于没配"的条件）
│   ├── llm_reviewer.py      分批送审 + 字段级容错
│   ├── merger.py            取高不取低 + 双来源留痕
│   └── global_checker.py    必备条款全局校验（元数据要求**按业务类型区分**）
└── llm/                     LLM 接入层
    ├── base.py              LLMProvider Protocol / ChatMessage / ChatResult
    ├── json_utils.py        JSON 四策略提取
    ├── openai_compat.py     真实端点（任意 OpenAI 兼容 base_url）
    ├── mock_provider.py     7 组预置 fixture（命中多组时**全部合并**并按 title 去重）
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

# 单元 + 集成测试（含 WPS COM 端到端，116 项 + 1 skip）
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
| 扫描件 / 图片 | PyMuPDF 渲染 + RapidOCR | 批次 8 起启用，开关 `OCR_ENABLED`（默认 true） |

**降级链**：`wps_com` → `libreoffice` → `passthrough`。实际链落库到 `parse_result.converter_fallback_chain`，用于解释报告里的页码语义。

**OCR 路径的三处接缝**（批次 8 补齐，此前是断的）：

| 接缝 | 不补会怎样 |
|---|---|
| `build_ocr_blocks`：OCR 行 → `ParsedBlock` | `split_clauses` 只遍历 `blocks`，不补则**切出 0 条条款**，任务却报 `completed` |
| `PageText.source='ocr'` + `confidence` | 条款/锚点会被标成 `native_text`，前端不提示"识别定位，可能有偏差" |
| `pdf_bytes`（图片路径回传包好的 PDF） | 图片合同无渲染用 PDF，工作台显示"正文无法渲染" |

**OCR 结果缓存**：按 `ocr:{file_hash}:{page}` 存 Redis（TTL 7 天）。实测同一份 2 页扫描件二次解析 **6.7s → 0.20s**。缓存读写失败只记警告，不影响 OCR 本身。

> ⚠️ **OCR 的坐标精度有固有上限**：RapidOCR 只给**行框**，不给字符级坐标。本项目按行内等宽切分给各字符，因此"高亮到某几个字"在扫描件上是**近似**的。风险项仍能准确定位到行/条款，但框的左右边界可能与实际字形有偏差。
>
> ⚠️ **扫描件没有 PDF.js 文本层**：工作台正文里点文字反向定位风险卡片的能力对扫描件不可用（文本层为空）；风险卡片 → 正文的正向高亮跳转不受影响。

### 4.2 规则引擎：5 种规则类型 × 4 种 metric（`rule_engine.py`）

| 规则类型 | 判据 | 条件运算符（真正被读取的） |
|---|---|---|
| `keyword` | `config.keywords`（+ `match_all`） | `contains` |
| `regex` | `config.pattern` | `regex` |
| `blacklist` | `config.blacklist` 条目 | `contains` / `regex` |
| `presence` | `config.required_pattern`，或 `within_clause_type` 内必须有该模式 | `not_exists`（`exists` 作为前置守卫亦接受） |
| `threshold` | 按 `metric` 分派，见下表 | 无（`exists` 前置守卫接受） |

**`threshold` 的四种 metric**（批次 9 起）：

| metric | 语义 | 需要 threshold | 关键实现 |
|---|---|---|---|
| `penalty_ratio` | 扫描**任意条款**第一个百分比，与上限比较 | ✅ | `_check_penalty_ratio` |
| `amount` | `metadata.amount` 与阈值比较 | ✅ | `_check_amount` |
| `probation_pay_ratio` | **只在含「试用期」的条款**里取百分比（《劳动合同法》第二十条 80% 下限） | ✅ | `_check_probation_pay` |
| `liability_asymmetry` | 全文**并存**重责表述（全部损失/无上限）与轻责表述（万分之一/累计不超过/比例 ≤ `mild_ratio_max`） | ❌ | `_check_liability_asymmetry` |

> **为什么 `probation_pay_ratio` 不复用 `penalty_ratio`**：后者扫任意条款，
> 在劳动合同里会抓到绩效比例、公积金比例这类无关数字。
>
> **为什么 `liability_asymmetry` 不需要 threshold**：它判的不是"某个值超限"，
> 而是"两种表述并存"。`_apply_threshold` 在取阈值**之前**分派它，
> 否则会被 threshold 校验拦掉。
>
> ⚠️ **不对等判定是有意的关键词并存启发式**，不做当事人方向理解。取舍与局限见
> `architecture.md` §18.2.5 与 R16。

**"配了等于没配"的防线**（`rule_config.py`，批次 9 收紧）：
写入时按规则类型校验条件运算符是否真被引擎读取——不在 `_READ_CONDITION_OPS`
且不属于 `_UNIVERSAL_CONDITION_OPS` 的一律 400。目的是让"规则已启用却从不命中"
在保存时暴露，而不是等审查漏报后才被发现。

### 4.3 锚点：四级降级（`anchor_builder.locate`）

| 顺序 | 方法 | 触发条件 | 产出 level |
|---|---|---|---|
| ①-a | `_match_raw` | 原文逐字包含 | `exact` |
| ①-b | `_match_compact` | 去空白后包含（跨行引用） | `exact` |
| ①-c | `_match_fragment` | 按标点切片段，锚定最长可定位片段 | 命中片段本身的 level，`partial=True` |
| ② | `_match_fuzzy` | 归一化 + 等长窗口相似度 ≥ 0.72 | `fuzzy` |
| ③ | `locate_clause` | 知道属于哪条条款但引用定位不到（PRESENCE 类规则） | `paragraph`，按页切分，覆盖整条条款 |
| ③' | `locate_block` | 条款正文整体无法精确定位时的兜底 | `paragraph`，仅覆盖起始块 |

**全部失败** → `level=none`，上层把 `risk_item.unanchored` 置 1，**不写 anchor 行、不伪造位置**。

### 4.4 合并：取高不取低

配对判据（`Merger._find_partner`，按优先级）：
1. 同一 `clause_index`
2. 标题归一化相似度 ≥ 0.55
3. 引用归一化相似度 ≥ 0.55

配对成功 → `merged_by=both`，等级取两者较高，两条依据都写入 `risk_evidence`。

### 4.5 闸门：防幻觉三条硬规则

1. JSON 解析失败必须重试或报错（在 provider 层）
2. 引用无法锚定必须标记（`unanchored=1`），不得丢弃或伪造
3. 法条无法在 `standard_clause.source` 中匹配 → `need_review=1`

---

## 5. 实测数据（4 份样本，批次 9 全绿）

```
采购 设备采购合同-高风险样本.docx   high / reject  高=4 中=2  共 6 项
采购 设备采购合同-扫描件.pdf        high / reject  高=4 中=2  共 6 项（走 OCR 链路）
销售 产品销售合同-高风险样本.docx   high / reject  高=3 中=2  共 5 项
劳动 劳动合同-高风险样本.docx       high / reject  高=4 中=2  共 6 项
```

| 样本 | seq | 等级 | 来源 | 锚定 | 风险项 |
|---|---|---|---|---|---|
| 采购 | 0 | high | both | paragraph | 未设置付款前置验收 |
| 采购 | 1 | high | both | exact | 违约责任无上限 |
| 采购 | 2 | high | both | exact | 管辖地约定违规 |
| 采购 | 3 | high | both | exact | 知识产权全部转让对方 |
| 采购 | 4 | medium | both | paragraph | 保密义务无期限 |
| 采购 | 5 | medium | both | paragraph | 不可抗力无通知时效 |
| 销售 | 0 | high | rule | exact | 违约责任不对等 |
| 销售 | 1 | high | both | exact | 违约责任无上限 |
| 销售 | 2 | high | both | exact | 管辖地约定违规 |
| 销售 | 3 | medium | both | paragraph | 保密义务无期限 |
| 销售 | 4 | medium | both | paragraph | 不可抗力无通知时效 |
| 劳动 | 0 | high | rule | exact | 试用期工资低于法定下限 |
| 劳动 | 1 | high | rule | exact | 约定放弃缴纳社会保险 |
| 劳动 | 2 | high | rule | exact | 约定不支付加班费 |
| 劳动 | 3 | high | both | exact | 对劳动者约定违约金 |
| 劳动 | 4 | medium | rule | paragraph | 竞业限制未约定补偿 |
| 劳动 | 5 | medium | rule | paragraph | 保密义务无期限 |

**采购样本的 `merged_by` 从批次 8 的 `rule`/`both` 混合变成全 `both`**：
批次 9 让 MockProvider 收集**全部**命中组（原先命中第一组就 return），
LLM 侧因此多回了"到货即付全款无验收条款"，与规则项合并成双来源。
风险总数不变（6 条），断言集按关键词匹配，**有意不校验 `merged_by`**（见 R17）。

> `paragraph` 级的项是 `GLOBAL` 类问题（必备条款缺失 / 无固定位置）。
> 它们锚定**整条条款**（含标题与正文；跨页条款按页各产出一个锚点），
> 因此点风险卡片时高亮的是完整条款，而不是只有标题行。
> 锚定对象是整条而非命中子串，按 `data-model.md` §5.7 的不变量不带字符区间。

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
| 9 | `LIABILITY_UNEQUAL` 与 `PENALTY_OVER_LIMIT` 配置完全相同 | 同一事实必报 high + medium 两条，"不对等"语义从未被判定 | 批次 9：新增 metric `liability_asymmetry`（跨条款重责/轻责并存） |
| 10 | 规则种子的"条件运算符"可配但引擎不读 | `blacklist` 只填 `contains` 而不填 `config.blacklist` 时正文明明含词却 0 命中 | 批次 9：`rule_config._READ_CONDITION_OPS` 按类型白名单，`_NEVER_READ_OPS` 直接拒 |
| 11 | `NO_ACCEPTANCE_BEFORE_PAY` 锚到 payment 类的**第一条** | 实测锚到"合同金额"而非"付款方式"；高亮指错段落，且与 LLM 同类结论无法合并 | 批次 9：`_guess_anchor_clause` 新增 `within + required_pattern + anchor_keywords` |
| 12 | 劳动合同必报"合同金额缺失" | 每份劳动合同稳定产生一条假阳性 | 批次 9：`global_checker` 必备元数据按业务类型区分（`_TRANSACTIONAL_TYPES`） |
| 13 | Mock 回放命中第一组即 return | 含多风险的条款只回一条，走不到多风险合并与双来源留痕 | 批次 9：`_match` 收集全部命中组并按 title 去重 |
| 14 | **WPS COM 并发转换必挂**（`Visible can not be set`） | mock 待办 1→3 条后，3 个并发转换线程必挂 1 个，合同被误判 `blocked: converter_unavailable` | 批次 9：`Dispatch` → `DispatchEx`（不复用已 Quit 的实例），并把 `Close`/`Quit` 移进 `_COM_LOCK` |

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
- **必须用 `DispatchEx` 而非 `Dispatch`**：后者是"连接已有实例"语义，前一个线程
  `Quit()` 后，后一个线程拿到的是**已被关掉的代理**，设 `Visible` 必失败。
  锁还必须覆盖 `Close` / `Quit`（完整生命周期），否则 B 线程会撞上 A 正在关闭的实例
- 分页结果与 Word/LibreOffice **可能不同**，报告须标注"页码基于 WPS 排版"

### 7.3 测试里的 ORM 缓存坑

测试用原始 SQL 改 `review_task.status` 后，**必须 `db.expire_all()`**：`SessionLocal` 配了 `expire_on_commit=False`，原始 SQL 绕过身份映射，不 expire 会读到过期对象，导致"非法流转"之类的假失败。

### 7.4 尚未做

PDF 报告精排、合同版本管理、批量审查（迁 Celery）、
`service` 模板的规则与样本、`compute_timeout` / `mark_stale_tasks` 接线（与 R11 同源）。

> 规则配置页随批次 7 交付、扫描件 OCR 链路随批次 8 交付、
> 销售/劳动样本与完整断言集随批次 9 交付，
> blocked 重试 UI 随工作台一并交付，均不在本清单。

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
