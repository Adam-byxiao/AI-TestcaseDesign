"""脚本：把仓库内的提示词与模板同步到包内 `src/prd2tc/assets/`。

为什么需要：`loader.load_asset` 在仓库内优先读 `prompts/`、`templates/`，
但**安装后回退到包内 `assets/`**。两处不一致会导致
「本地改了提示词、安装用户拿到的还是旧的」。

实测教训（两件，都不是假想）：
1. 本仓库曾有 3 个提示词文件不同步，且没有任何机制发现它；
2. 模板侧长期**没有纳入本脚本**，导致 `assets/templates/` 缺少
   `test-case.template.md` —— 仓库里有、安装后没有。因此现在两类资产统一处理。

用法：
    python scripts/sync_prompts.py          # 同步
    python scripts/sync_prompts.py --check  # 只检查（CI 用），不一致则退出码 1
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# (源目录, 目标目录, 展示名) —— 两类资产走同一条同步与校验逻辑
PAIRS = [
    (REPO / "prompts", REPO / "src" / "prd2tc" / "assets" / "prompts", "提示词"),
    (REPO / "templates", REPO / "src" / "prd2tc" / "assets" / "templates", "模板"),
]


def sync_one(src_dir: Path, dst_dir: Path, label: str, *, check_only: bool) -> int:
    if not src_dir.is_dir():
        print(f"✗ {label}源目录不存在: {src_dir}")
        return 2
    if not check_only:
        dst_dir.mkdir(parents=True, exist_ok=True)

    changed: list[str] = []
    missing: list[str] = []
    for src in sorted(src_dir.glob("*.md")):
        dst = dst_dir / src.name
        if check_only:
            if not dst.is_file() or dst.read_bytes() != src.read_bytes():
                changed.append(src.name)
            continue
        if not dst.is_file() or dst.read_bytes() != src.read_bytes():
            dst.write_bytes(src.read_bytes())
            changed.append(src.name)

    # 反向检查：assets 里多出来的文件（源已删除）
    for dst in sorted(dst_dir.glob("*.md")):
        if not (src_dir / dst.name).is_file():
            missing.append(dst.name)

    if check_only:
        if changed or missing:
            print(f"✗ {label}副本不同步：")
            for name in changed:
                print(f"    {name}: 与 {src_dir.name}/ 不一致或缺失")
            for name in missing:
                print(f"    {name}: assets 中存在但 {src_dir.name}/ 已无")
            return 1
        print(f"✓ {label}副本一致（{len(list(src_dir.glob('*.md')))} 个文件）")
        return 0

    print(f"✓ 已同步 {len(changed)} 个{label}到 {dst_dir.relative_to(REPO)}"
          + (f"：{', '.join(changed)}" if changed else "（无变化）"))
    if missing:
        print(f"  ⚠ assets 中多出（源已删除，需手工确认）：{', '.join(missing)}")
    return 0


def main() -> int:
    check_only = "--check" in sys.argv
    codes = [sync_one(src, dst, label, check_only=check_only) for src, dst, label in PAIRS]
    if check_only and any(code != 0 for code in codes):
        print("\n  运行 `python scripts/sync_prompts.py` 修复。")
        return 1
    return max(codes, default=0)


if __name__ == "__main__":
    raise SystemExit(main())
