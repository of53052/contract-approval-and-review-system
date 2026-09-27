# 数据层操作手册

> 面向开发者的日常操作指引。设计依据：[`docs/data-model.md`](data-model.md)。
> 本文件只讲**怎么用**，设计理由看文档。

---

## 1. 前置条件

基础设施（MySQL / Redis / MinIO）跑在 Docker 里，后端在宿主机原生运行：

```powershell
# 启动基础设施（项目根目录）
docker compose up -d

# 确认三个容器都 healthy
docker compose ps
```

数据库连接（端口刻意错开本机 FlyEnv 的 3306/6379）：

| 项 | 值 |
|---|---|
| 地址 | `127.0.0.1:13306` |
| 库 | `contract_review` |
| 用户 | `contract_review` / `cr_pass_2026` |
| 字符集 | `utf8mb4` / `utf8mb4_0900_ai_ci` |

凭据统一来自项目根 `.env`，**不在代码或 `alembic.ini` 中硬编码**。

---

## 2. 日常命令速查

先设一个变量指向 backend 的虚拟环境，命令统一在**项目根目录**执行：

```powershell
$py = "backend\.venv\Scripts\python.exe"
```

| 目的 | 命令 |
|---|---|
| 建表 / 升级到最新 | `& $py -m alembic -c backend\alembic.ini upgrade head` |
| 检查是否有未生成的模型变更 | `& $py -m alembic -c backend\alembic.ini check` |
| 生成新迁移 | `& $py -m alembic -c backend\alembic.ini revision --autogenerate -m "描述"` |
| 回退一步 | `& $py -m alembic -c backend\alembic.ini downgrade -1` |
| 写入种子数据（幂等） | `& $py scripts\seed_data.py` |
| 一致性检查 C1~C8 | `& $py scripts\check_consistency.py` |
| 环境自检 | `& $py scripts\check_env.py` |
| 数据层测试 | `& $py -m pytest backend\tests -q` |

> 统一在**项目根目录**执行，用 `-c backend\alembic.ini` 显式指定配置，
> 不必 `cd`。`alembic/env.py` 会自己把 `backend/` 加进 `sys.path`，
> 且 `script_location` 是相对 ini 文件的路径，因此从哪执行都不受影响。

---

## 3. 改表结构的正确姿势

**不要手写 DDL，也不要直接改数据库。** 流程是：

1. 改 `backend/app/models/` 下的模型
2. `alembic revision --autogenerate -m "..."` 生成迁移
3. **人工审查**生成的迁移文件（autogenerate 会漏掉某些变更）
4. `alembic upgrade head`
5. `alembic check` 确认无漂移

### 已知的 autogenerate 盲区

| 场景 | 表现 | 处理 |
|---|---|---|
| `Computed` 生成列变更 | 告警 `Computed default ... cannot be modified`，不生成操作 | 手写迁移 |
| `server_onupdate` | **完全不渲染进 DDL** | 改用 `server_default=text("CURRENT_TIMESTAMP(3) ON UPDATE ...")` |
| 索引重命名 | 表现为 drop + create | 手工改为 `op.execute("ALTER TABLE ... RENAME INDEX ...")` |
| `downgrade` 里的 `drop_index` | 支撑外键的索引删不掉（1553） | 删掉这些 `drop_index`，只留 `drop_table` |
| 中文注释列 | 正常生成 | — |

---

## 4. 约定（写模型时必须遵守）

### 4.1 列类型一律用 `app/models/types.py` 的工厂

不要直接写 `mapped_column(String(32))` 这类裸类型：

| 需求 | 用 |
|---|---|
| 主键 | `pk_col()` |
| 外键 | `fk_col("contract.id")` |
| 枚举 | `enum_col(TaskStatus, default=TaskStatus.PENDING)` |
| 布尔 | `bool_col()` |
| 金额 | `money_col()` |
| 创建时间 | `created_at_col()` |
| 更新时间 | `updated_at_col()` |

**新增枚举必须同时在 `enums.py` 的 `ENUM_WIDTH` 登记列宽**，
否则 `enum_col` 会直接抛错。这是刻意的：列宽不能靠"当前最长值"推导，
否则新增枚举值就要改 DDL，违背原则 P2。

### 4.2 默认值要同时设 Python 侧与数据库侧

只设 `default=`（Python 侧）时，绕过 ORM 直插会撞 NOT NULL。
`enum_col` / `bool_col` / `updated_at_col` 已处理，自定义列需自行注意。

### 4.3 命名

按 `docs/data-model.md §1.1`：表名单数小写下划线，外键 `{单数表名}_id`，
索引 `idx_` / 唯一 `uk_` / 外键 `fk_`。约束名由 `MetaData` 的
`naming_convention` 统一生成，不要手工指定。

---

## 5. 踩过的坑（都已在代码中修正，勿回退）

| # | 坑 | 现象 | 修正 |
|---|---|---|---|
| 1 | 可空列进唯一键 | 重复插入不被拒，去重完全失效 | `deleted_key` 生成列归一化 NULL |
| 2 | `server_onupdate` | DDL 里没有 `ON UPDATE`，时间戳不刷新 | 写进 `server_default` |
| 3 | `NOW(3)` vs `CURRENT_TIMESTAMP(3)` | `alembic check` 永远报漂移 | 统一用 `CURRENT_TIMESTAMP(3)` |
| 4 | 枚举列宽自动推导 | 新增枚举值需 DDL 迁移 | `ENUM_WIDTH` 显式登记 |
| 5 | `alembic.ini` 写中文注释 | configparser 以 GBK 读取，`UnicodeDecodeError` | ini 保持纯 ASCII |
| 6 | `enum_col` 只设 Python 默认值 | 直插报 `Column 'source' cannot be null` | 补 `server_default` |
| 7 | autogenerate 的 `downgrade` 显式 `drop_index` | 回滚报 1553 `Cannot drop index ... needed in a foreign key constraint` | 只按逆拓扑序 `drop_table`，索引随表一起删 |

---

## 6. 从零重建数据库

```powershell
$py = "backend\.venv\Scripts\python.exe"

# 1. 删库重来（会清空所有业务数据，仅开发环境使用）
docker compose down -v
docker compose up -d

# 2. 等三个容器都 healthy
docker compose ps

# 3. 建表
& $py -m alembic -c backend\alembic.ini upgrade head

# 4. 种子数据
& $py scripts\seed_data.py

# 5. 验证
& $py scripts\check_env.py
& $py scripts\check_consistency.py
```

> `docker compose down -v` 会删除 named volume 中的**全部**数据。
> 只在本机开发环境执行，且确认没有需要保留的数据。
