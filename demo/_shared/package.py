"""把 `_shared/` vendor 進單一 demo，產出可獨立交付的資料夾。

用途：客戶買的是「一個模組」，不是整個 repo。交付時把共用層複製進該 demo，
對方拿到的資料夾自己就能跑，不需要理解 `demo/` 的目錄結構。

    python package.py demo01-morning-briefing --out dist/

產出佈局：

    dist/demo01-morning-briefing/
    ├── _shared/            # 由本工具 vendor 進來
    ├── main.py             # sys.path 插入路徑已改寫為自身目錄
    ├── config.yaml
    ├── prompts/ mock/ ...
    ├── requirements.txt    # 由 demo/ 根目錄複製
    └── .env.example

本檔刻意只用標準庫、也不 import 同層的 `config_loader`：
交付前的打包動作不該因為對方還沒 `pip install PyYAML` 就失敗。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

SHARED_DIR_NAME = "_shared"
# 這些是執行期垃圾或版控目錄，不該進交付包。
EXCLUDED_NAMES = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".git", ".venv", "dist", "build",
})
# 打包工具本身不必交付給客戶。
SHARED_EXCLUDED_FILES = frozenset({"package.py"})
# 從 demo/ 根目錄一併複製的輔助檔。
EXTRA_ROOT_FILES = ("requirements.txt", ".env.example")

# demo 的 main.py / test_main.py 原本插入「上一層」（demo/）好 import `_shared`；
# vendor 之後 `_shared` 就在自身目錄底下，因此要少退一層。
_PARENT_PARENT = re.compile(r"Path\(__file__\)\.resolve\(\)\.parent\.parent")
_PARENT = "Path(__file__).resolve().parent"


def project_root() -> Path:
    """demo/ 的絕對路徑（以本檔位置推算，禁止硬編碼）。"""
    return Path(__file__).resolve().parent.parent


def _ignore(_directory: str, names: list[str]) -> set[str]:
    """copytree 的忽略規則。"""
    return {name for name in names if name in EXCLUDED_NAMES}


def _ignore_shared(_directory: str, names: list[str]) -> set[str]:
    """vendor `_shared/` 時額外排除打包工具本身。"""
    return {name for name in names if name in EXCLUDED_NAMES or name in SHARED_EXCLUDED_FILES}


def _resolve_source(demo_name: str) -> Path:
    """驗證 demo 目錄存在且看得出是一個 demo（有 main.py）。"""
    root = project_root()
    source = (root / demo_name).resolve()
    if not source.is_dir():
        available = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.startswith("demo"))
        raise FileNotFoundError(
            f"找不到 demo 目錄：{source}\n可用的 demo：{', '.join(available) or '（尚未建立）'}"
        )
    if not (source / "main.py").is_file():
        raise FileNotFoundError(f"{source} 底下沒有 main.py，看起來不是一個 demo 目錄")
    return source


def _prepare_destination(source: Path, out_dir: Path, demo_name: str, force: bool) -> Path:
    """算出並清理輸出目錄，順便擋掉「輸出到來源目錄裡面」這種會無限遞迴的用法。"""
    destination = (out_dir / demo_name).resolve()
    if destination == source or source in destination.parents:
        raise ValueError(f"輸出目錄不可位於來源 demo 之內：{destination}")
    if destination.exists():
        if not force:
            raise FileExistsError(f"輸出目錄已存在：{destination}（要覆蓋請加 --force）")
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def rewrite_sys_path(package_dir: Path) -> list[Path]:
    """把 demo 層 .py 的 sys.path 插入路徑改成自身目錄，回傳有改動的檔案清單。

    `_shared/` 底下的檔案一律不改：`config_loader.project_root()` 的
    `parent.parent` 在 vendor 後正好指向交付包根目錄，本來就是對的。
    """
    changed: list[Path] = []
    for py_file in sorted(package_dir.rglob("*.py")):
        if SHARED_DIR_NAME in py_file.relative_to(package_dir).parts:
            continue
        original = py_file.read_text(encoding="utf-8")
        rewritten = _PARENT_PARENT.sub(_PARENT, original)
        if rewritten != original:
            py_file.write_text(rewritten, encoding="utf-8")
            changed.append(py_file)
    return changed


def _copy_extra_files(destination: Path) -> list[str]:
    """複製 demo/ 根目錄的 requirements.txt 與 .env.example（缺檔就略過）。"""
    copied: list[str] = []
    for name in EXTRA_ROOT_FILES:
        source_file = project_root() / name
        if source_file.is_file():
            shutil.copy2(source_file, destination / name)
            copied.append(name)
    return copied


def build_package(demo_name: str, out_dir: Path, force: bool = False, dry_run: bool = False) -> Path:
    """產出單一 demo 的獨立交付包，回傳輸出目錄路徑。"""
    source = _resolve_source(demo_name)
    destination = _prepare_destination(source, out_dir, demo_name, force)
    if dry_run:
        print(f"[DRY-RUN] 來源：{source}")
        print(f"[DRY-RUN] 輸出：{destination}")
        print(f"[DRY-RUN] 會 vendor：{project_root() / SHARED_DIR_NAME}")
        return destination

    shutil.copytree(source, destination, ignore=_ignore)
    shutil.copytree(
        project_root() / SHARED_DIR_NAME,
        destination / SHARED_DIR_NAME,
        ignore=_ignore_shared,
    )
    changed = rewrite_sys_path(destination)
    extras = _copy_extra_files(destination)

    print(f"[OK] 交付包已產出：{destination}")
    print(f"     改寫 sys.path 的檔案：{len(changed)} 個"
          + (f"（{', '.join(p.name for p in changed)}）" if changed else ""))
    print(f"     附帶檔案：{', '.join(extras) or '（無）'}")
    print(f"     驗收指令：cd \"{destination}\" && python main.py --mock")
    return destination


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器。"""
    parser = argparse.ArgumentParser(
        prog="package.py",
        description="把 _shared/ vendor 進指定 demo，產出可獨立交付的資料夾",
    )
    parser.add_argument("demo", help="demo 目錄名稱，例：demo01-morning-briefing")
    parser.add_argument("--out", default="dist", help="輸出根目錄（預設：dist）")
    parser.add_argument("--force", action="store_true", help="輸出目錄已存在時覆蓋")
    parser.add_argument("--dry-run", action="store_true", help="只顯示會做什麼，不實際複製")
    return parser


def main(argv: list[str] | None = None) -> int:
    """解析參數 -> 打包 -> 回傳 exit code。"""
    args = build_parser().parse_args(argv)
    try:
        build_package(
            demo_name=args.demo.rstrip("/\\"),
            out_dir=Path(args.out).expanduser(),
            force=args.force,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, FileExistsError, ValueError, OSError) as exc:
        print(f"[ERROR] 打包失敗：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
