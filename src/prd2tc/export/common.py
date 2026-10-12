"""导出共享工具：行展开、文本渲染、二进制原子写、ZIP 打包。

与 `io.atomic_write_text` 的分工：文本写盘用 io，二进制用本模块。
ZIP 打包固定时间戳与成员顺序，保证「同输入 → 同字节」的可复现性。
"""
from __future__ import annotations

import io
import os
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

# XLSX / XMIND 的固定时间戳：保证同输入产出字节一致（可复现性要求）
FIXED_TIMESTAMP = (2020, 1, 1, 0, 0, 0)

EMPTY = "—"


def list_to_text(values: list[Any] | None, *, sep: str = "\n", empty: str = EMPTY) -> str:
    """字符串数组 → 多行文本；空数组返回占位符。"""
    items = [str(v) for v in (values or []) if str(v).strip()]
    return sep.join(items) if items else empty


def render_steps(steps: list[dict] | None) -> str:
    """步骤数组 → 编号文本：`1. [action] target ← data`。

    动作加方括号便于在 Excel 中一眼区分动作与定位目标；
    有输入值时用 ← 标注，符合测试人员阅读习惯。
    """
    if not steps:
        return EMPTY
    lines = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            lines.append(f"{index}. {step}")
            continue
        action = str(step.get("action") or "").strip()
        target = str(step.get("target") or "").strip()
        data = str(step.get("data") or "").strip()
        line = f"{index}. [{action}] {target}" if action else f"{index}. {target}"
        if data:
            line += f" ← {data}"
        lines.append(line)
    return "\n".join(lines)


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """二进制原子写：先写临时文件再 os.replace。

    与 io.atomic_write_text 同样的 Windows 兼容处理——服务层的心跳与状态轮询
    会并发读写同一目录，os.replace 可能因目标文件被占用抛 PermissionError，
    短暂重试即可让读者让出。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    return path


def build_zip_bytes(members: dict[str, str]) -> bytes:
    """按固定顺序与固定时间戳生成 ZIP 字节，确保可复现（纯函数，不写盘）。"""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    return stream.getvalue()


def write_zip(path: Path, members: dict[str, str]) -> Path:
    """生成 ZIP 并原子写盘。"""
    return atomic_write_bytes(path, build_zip_bytes(members))
