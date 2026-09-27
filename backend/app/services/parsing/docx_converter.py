"""DOCX → PDF 转换器（策略模式 + 三级降级链）。

设计依据：docs/architecture.md §5.4。

> ⚠️ **DOCX 本身不含分页信息**（`<w:pageBreak>` = 0、python-docx 无页码 API）。
> 真实页码必须由排版引擎渲染后才可知。

降级链：`WpsComConverter` → `LibreOfficeConverter` → `PassthroughConverter`

**关键约束**：WPS COM 依赖**交互式桌面会话**，若后端跑成 Windows 服务会失败；
且实测多线程下必须先 `pythoncom.CoInitialize()`，否则必失败。

**不静默降级**：任一环节失败都记录原因，返回的 `ConversionResult` 带完整降级链，
落库到 `parse_result.converter_fallback_chain`，否则报告里的页码无法解释
（实测 WPS 3 页 vs LibreOffice 2 页）。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

#: COM 调用全局互斥锁。
#: 实测：多个线程同时调 WPS COM 会失败，即使各自 CoInitialize 也不稳；
#: 用串行化换取可靠性——演示场景并发量为 1，代价可接受。
_COM_LOCK = threading.Lock()


@dataclass
class ConversionResult:
    """转换结果。`pdf_bytes` 为 None 表示全部转换器都失败。"""

    pdf_bytes: bytes | None
    converter_used: str | None
    fallback_chain: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.pdf_bytes is not None

    @property
    def chain_str(self) -> str:
        """降级链字符串，落库用。"""
        return ">".join(self.fallback_chain)


class DocxConverter(Protocol):
    """DOCX 转换器接口。"""

    name: str

    def is_available(self) -> bool: ...

    def to_pdf(self, docx_path: Path) -> bytes: ...


class WpsComConverter:
    """用 WPS 的 Word COM 接口导出 PDF。

    这是**首选**方案：用户日常用 WPS 看文档，所见即所得。
    """

    name = "wps_com"

    def is_available(self) -> bool:
        import sys

        if sys.platform != "win32":
            return False
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
        except ImportError:
            return False
        return True

    def to_pdf(self, docx_path: Path) -> bytes:
        import pythoncom
        import win32com.client

        # 必须在**本线程**初始化 COM；跨线程复用会失败（实测）
        pythoncom.CoInitialize()
        app = None
        doc = None
        try:
            # 全局串行化，避免并发调用 COM 失败
            with _COM_LOCK:
                app = win32com.client.Dispatch("Word.Application")
                app.Visible = False
                app.DisplayAlerts = False
                doc = app.Documents.Open(str(docx_path.resolve()), ReadOnly=True)
                with tempfile.TemporaryDirectory() as tmp:
                    pdf_path = Path(tmp) / "out.pdf"
                    # FileFormat=17 即 wdFormatPDF
                    doc.SaveAs2(str(pdf_path), FileFormat=17)
                    data = pdf_path.read_bytes()
            return data
        finally:
            # 显式关闭，否则会残留 wps.exe 进程（实测发现）
            try:
                if doc is not None:
                    doc.Close(SaveChanges=0)
            except Exception as exc:  # noqa: BLE001 - 清理失败不应掩盖主异常
                logger.warning("关闭 WPS 文档失败: %s", exc)
            try:
                if app is not None:
                    app.Quit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("退出 WPS 进程失败: %s", exc)
            # ⚠️ 已知噪声：释放已 Quit 的 COM 代理时，pywin32 会在
            # stderr 打印 `Windows fatal exception: code 0x800706be`
            # （RPC_S_CALL_FAILED）。实测为无害——进程退出码正常、
            # wps.exe 会在数秒内自行退出，且尝试过多种释放顺序均无法消除。
            # 它只在 faulthandler 启用时可见（pytest 默认启用）。
            pythoncom.CoUninitialize()


class LibreOfficeConverter:
    """用 LibreOffice 命令行导出 PDF。

    ⚠️ 分页结果与 WPS **可能不同**（实测 2 页 vs 3 页），因此仅作降级备选。
    """

    name = "libreoffice"

    #: 常见安装路径（Windows）
    _CANDIDATES = (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    )

    def _find_binary(self) -> str | None:
        for cand in self._CANDIDATES:
            if Path(cand).exists():
                return cand
        return shutil.which("soffice") or shutil.which("libreoffice")

    def is_available(self) -> bool:
        return self._find_binary() is not None

    def to_pdf(self, docx_path: Path) -> bytes:
        binary = self._find_binary()
        if binary is None:
            raise RuntimeError("未找到 LibreOffice 可执行文件")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            proc = subprocess.run(  # noqa: S603 - 参数为固定模板，无 shell 注入面
                [binary, "--headless", "--convert-to", "pdf", "--outdir",
                 str(out_dir), str(docx_path.resolve())],
                capture_output=True,
                timeout=120,
                check=False,
            )
            pdf_path = out_dir / (docx_path.stem + ".pdf")
            if not pdf_path.exists():
                raise RuntimeError(
                    f"LibreOffice 未产出 PDF（退出码 {proc.returncode}）: "
                    f"{proc.stderr.decode('utf-8', 'replace')[:200]}"
                )
            return pdf_path.read_bytes()


class PassthroughConverter:
    """兜底转换器：不做转换。

    语义是"放弃分页"，由调用方改走段落级锚点（`anchor_level=paragraph`）。
    它的 `to_pdf` 必然失败，但 `is_available` 恒为 True——以此保证降级链有终点。
    """

    name = "passthrough"

    def is_available(self) -> bool:
        return True

    def to_pdf(self, docx_path: Path) -> bytes:
        raise RuntimeError("passthrough 不产生 PDF，DOCX 将降级为段落级锚点")


#: 降级链顺序（按架构文档 §5.4）
DEFAULT_CHAIN: tuple[type, ...] = (
    WpsComConverter,
    LibreOfficeConverter,
    PassthroughConverter,
)


def convert_docx(
    docx_path: Path,
    *,
    preferred: str | None = None,
    chain: tuple[type, ...] = DEFAULT_CHAIN,
) -> ConversionResult:
    """按降级链尝试把 DOCX 转成 PDF。

    `preferred` 来自配置 `DOCX_CONVERTER`（默认 `wps_com`）：
    指定哪个转换器**优先**尝试，其余仍按链顺序兜底。

    返回的 `fallback_chain` 记录**实际尝试过**的转换器顺序与结果，
    落库后即可解释报告中的页码语义来源。
    """
    docx_path = Path(docx_path)
    if not docx_path.exists():
        raise FileNotFoundError(f"DOCX 不存在: {docx_path}")

    converters: list[DocxConverter] = [cls() for cls in chain]
    if preferred:
        # 把首选提到最前，其余保持链顺序
        converters.sort(key=lambda c: 0 if c.name == preferred else 1)

    result = ConversionResult(pdf_bytes=None, converter_used=None)
    for conv in converters:
        if not conv.is_available():
            reason = f"{conv.name}: 不可用"
            logger.info("跳过转换器 %s（不可用）", conv.name)
            result.fallback_chain.append(conv.name)
            result.errors.append(reason)
            continue
        try:
            data = conv.to_pdf(docx_path)
            if not data:
                raise RuntimeError("转换产出为空")
            result.pdf_bytes = data
            result.converter_used = conv.name
            result.fallback_chain.append(conv.name)
            logger.info("DOCX 转换成功: %s（链: %s）", conv.name, result.chain_str)
            return result
        except Exception as exc:  # noqa: BLE001 - 降级链需要吞掉单点失败并继续
            reason = f"{conv.name}: {type(exc).__name__}: {exc}"
            logger.warning("转换器 %s 失败，继续降级: %s", conv.name, exc)
            result.fallback_chain.append(conv.name)
            result.errors.append(reason)

    logger.error("全部转换器失败: %s", result.errors)
    return result
