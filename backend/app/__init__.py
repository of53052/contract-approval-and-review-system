# -*- coding: utf-8 -*-
"""合同审批审查系统后端包。

**版本号唯一来源**：本模块的 `__version__` 是全项目版本号的唯一出处。
`pyproject.toml` 通过 `[tool.hatch.version]` 动态读取它（不再重复写死），
`app/main.py` 的 FastAPI 元信息与 `/health` 响应也从此导入。

改版本号时只改这一处，避免"多份副本各写各的"导致漂移
（历史上 `pyproject.toml` 停在 0.1.0 而运行时报 0.4.0）。
"""

__version__ = "0.4.0"
