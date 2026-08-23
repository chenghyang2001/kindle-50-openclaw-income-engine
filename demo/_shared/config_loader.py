"""YAML 設定載入 + `${ENV_VAR}` 展開 + 必要環境變數驗證。

刻意不提供「找不到環境變數就用預設值」的行為：書中第 04 章的紅色警報有一半
都源自「憑證其實沒設定，但程式用空字串繼續跑」，錯誤因此延後好幾層才爆炸。
這裡的原則是缺什麼就當場列出來、當場停下。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# 只認 ${VAR} 這一種寫法（不支援 $VAR），避免 YAML 裡的貨幣符號或 shell 片段被誤展開。
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def project_root() -> Path:
    """回傳 demo/ 的絕對路徑（以本檔位置推算，禁止硬編碼）。

    正常佈局 `demo/_shared/config_loader.py` -> `demo/`；
    被 `package.py` vendor 後的佈局 `<交付包>/_shared/config_loader.py` -> `<交付包>/`。
    兩種情況都指向「本套件所屬的專案根目錄」，因此不需要在打包時改寫這一行。
    """
    return Path(__file__).resolve().parent.parent


def expand_env(value: Any, missing: list[str] | None = None) -> Any:
    """遞迴展開字串中的 `${ENV_VAR}`；環境變數不存在則保留原樣並記錄到 missing。

    保留原樣（而非換成空字串）是刻意的：下游的 `Notifier._credential` 看到
    仍是 `${...}` 開頭就知道「這格根本沒被填」，而不是誤以為使用者設了空值。
    """
    if isinstance(value, str):
        return _expand_string(value, missing)
    if isinstance(value, dict):
        return {key: expand_env(item, missing) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item, missing) for item in value]
    return value


def _expand_string(value: str, missing: list[str] | None) -> str:
    """單一字串的展開。"""
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        env_value = os.environ.get(name)
        if env_value is None:
            if missing is not None and name not in missing:
                missing.append(name)
            return match.group(0)
        return env_value

    return _ENV_PATTERN.sub(replace, value)


def _read_yaml(path: Path) -> dict:
    """讀 YAML 並確保頂層是 mapping。"""
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ValueError(f"設定檔 YAML 格式錯誤：{path}｜{exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"設定檔不是 UTF-8 編碼：{path}｜{exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"設定檔頂層必須是 mapping，實際為 {type(data).__name__}：{path}")
    return data


def _verify_required_env(required_env: list[str]) -> None:
    """必要環境變數有缺就列出清單並結束行程（不可用預設值靜默掩蓋）。"""
    missing = [name for name in required_env if not os.environ.get(name, "").strip()]
    if not missing:
        return
    print("[RED] [config_loader] 缺少必要環境變數，無法繼續：", file=sys.stderr, flush=True)
    for name in missing:
        print(f"       - {name}", file=sys.stderr, flush=True)
    print("       對策：設定後重跑（PowerShell: setx <NAME> \"<VALUE>\"）", file=sys.stderr, flush=True)
    sys.exit(1)


def load_config(
    path: str | Path,
    required_env: list[str] | None = None,
) -> dict:
    """載入 demo 的 config.yaml。

    1. 讀 YAML（encoding="utf-8"）
    2. 遞迴展開字串中的 ${ENV_VAR}；環境變數不存在則保留原樣並記錄
    3. required_env 有缺 -> print 缺少清單到 stderr 後 sys.exit(1)
    4. 檔案不存在 -> 拋 FileNotFoundError，訊息含絕對路徑
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"找不到設定檔：{resolved}")

    raw = _read_yaml(resolved)
    unresolved: list[str] = []
    config = expand_env(raw, unresolved)
    if unresolved:
        print(
            f"[AMBER] [config_loader] 設定檔中有 {len(unresolved)} 個環境變數未設定，"
            f"已保留原樣：{', '.join(unresolved)}",
            file=sys.stderr,
            flush=True,
        )
    if required_env:
        _verify_required_env(list(required_env))
    return config
