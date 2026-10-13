"""脚本：把 `prompts/*.md` 同步到 `src/prd2tc/assets/prompts/`。

为什么需要：loader 在仓库内优先读 `prompts/`，但**安装后回退到包内 `assets/prompts/`**。
两处不一致会导致「本地改了提示词、安装用户拿到的还是旧的」——
实测本仓库曾有 3 个文件不同步，且没有任何机制发现它。

用法：
    python scripts/sync_prompts.py          # 同步
    python scripts/sync_prompts.py --check  # 只检查（CI 用），不一致则退出码 1
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "prompts"
DST = REPO / "src" / "prd2tc" / "assets" / "prompts"


def main() -> int:
    check_only = "--check" in sys.argv
    if not SRC.is_dir():
        print(f"✗ 源目录不存在: {SRC}")
        return 2
    if not check_only:
        DST.mkdir(parents=True, exist_ok=True)

    changed: list[str] = []
    missing: list[str] = []
    for src in sorted(SRC.glob("*.md")):
        dst = DST / src.name
        if check_only:
            if not dst.is_file() or dst.read_bytes() != src.read_bytes():
                changed.append(src.name)
            continue
        if not dst.is_file() or dst.read_bytes() != src.read_bytes():
            dst.write_bytes(src.read_bytes())
            changed.append(src.name)

    # 反向检查：assets 里多出来的文件（源已删除）
    for dst in sorted(DST.glob("*.md")):
        if not (SRC / dst.name).is_file():
            missing.append(dst.name)

    if check_only:
        if changed or missing:
            print("✗ 提示词副本不同步：")
            for name in changed:
                print(f"    {name}: 与 prompts/ 不一致或缺失")
            for name in missing:
                print(f"    {name}: assets 中存在但 prompts/ 已无")
            print("\n  运行 `python scripts/sync_prompts.py` 修复。")
            return 1
        print("✓ 提示词副本一致")
        return 0

    print(f"✓ 已同步 {len(changed)} 个文件到 assets/prompts/"
          + (f"：{', '.join(changed)}" if changed else "（无变化）"))
    if missing:
        print(f"  ⚠ assets 中多出（源已删除，需手工确认）：{', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
