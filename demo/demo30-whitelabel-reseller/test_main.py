"""模組 #30 的三個測試（happy / edge / integration）。

integration 是本模組最重要的一個：它同時驗證兩條鐵律——
「A 經銷商拿不到 B 經銷商的資料」與「分潤拆帳分毫不差且總和為 100%」。
這兩條只要破一條，白牌商業模式就不成立。
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main as demo30  # noqa: E402
from audit import AuditLog  # noqa: E402
from revenue_share import SplitPolicy, split_fee  # noqa: E402
from tenancy import IsolationViolation, TenantStore, parse_namespace  # noqa: E402

BASE_CONFIG_PATH = MODULE_DIR / "config.yaml"
ACME = "acme-ops"
BRIGHTPATH = "brightpath-ai"


def _load_base_config() -> dict:
    """讀出正式 config.yaml 作為各測試的基底。"""
    return yaml.safe_load(BASE_CONFIG_PATH.read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, config: dict) -> Path:
    """把改過的設定寫成臨時 config.yaml，避免測試污染正式設定。"""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _run(tmp_path: Path, config_path: Path | None = None, extra: list[str] | None = None) -> dict:
    """以 --mock 跑一次主流程，稽核與狀態檔一律導向 tmp_path。"""
    argv = [
        "--mock",
        "--notify",
        "console",
        "--audit-file",
        str(tmp_path / "audit.jsonl"),
        "--state-file",
        str(tmp_path / "state.json"),
    ]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    argv += extra or []
    return demo30.run(demo30.build_parser().parse_args(argv))


def _by_slug(result: dict, slug: str) -> dict:
    """從結果取出指定經銷商的區塊。"""
    for reseller in result["resellers"]:
        if reseller["reseller_slug"] == slug:
            return reseller
    raise AssertionError(f"結果中找不到經銷商 {slug}")


def test_happy_path(tmp_path: Path) -> None:
    """標準 mock 輸入：2 個經銷商 × 2 個終端客戶，安全閥全過，稽核與狀態檔落地。"""
    result = _run(tmp_path)

    assert result["module_id"] == "30"
    assert result["mode"] == "mock"
    assert result["dry_run"] is False
    # 內部回收工時原簡報未提供，必須如實標示而非填 0 或推估值
    assert result["recovered_hours_per_month"] == "（原簡報未提供）"

    # 全域安全閥：對外呼叫前的內部 dry-run 通訊測試必須全數通過
    assert result["selftest"]["required"] is True
    assert result["selftest"]["passed"] == result["selftest"]["total"] == 4

    assert {item["reseller_slug"] for item in result["resellers"]} == {ACME, BRIGHTPATH}
    namespaces = {
        item["namespace"] for reseller in result["resellers"] for item in reseller["sub_clients"]
    }
    assert namespaces == {
        f"{ACME}/north-mill",
        f"{ACME}/harbor-freight-co",
        f"{BRIGHTPATH}/lakeside-clinic",
        f"{BRIGHTPATH}/north-mill",
    }

    # 品牌層整組抽換：每個經銷商掛自己的品牌，且對外輸出沒有夾帶提供者身分
    assert _by_slug(result, ACME)["brand_display_name"] == "Acme Operations Cloud"
    assert _by_slug(result, BRIGHTPATH)["brand_display_name"] == "BrightPath Operations Suite"
    for reseller in result["resellers"]:
        for item in reseller["sub_clients"]:
            assert item["brand_leaks"] == []

    assert result["state_written"] is True
    assert (tmp_path / "state.json").is_file()
    assert AuditLog.load(tmp_path / "audit.jsonl")


def test_edge_case_namespace_escape_is_rejected(tmp_path: Path) -> None:
    """邊界：--tenant 帶路徑跳脫字元 → 拒絕執行、發 RED、留稽核，且絕不嘗試修正。"""
    with pytest.raises(SystemExit) as excinfo:
        _run(tmp_path, extra=["--tenant", f"{ACME}/../{BRIGHTPATH}"])
    assert excinfo.value.code == 1

    entries = AuditLog.load(tmp_path / "audit.jsonl")
    rejected = [item for item in entries if item["event"] == "namespace_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["severity"] == "red"
    assert rejected[0]["detail"]["source"] == "--tenant"
    assert ".." in rejected[0]["namespace"]

    # 拒絕發生在任何租戶資料被讀取之前
    assert not [item for item in entries if item["event"] == "tenant_read"]
    assert not (tmp_path / "state.json").exists()

    # 同一份輸入直接進解析器也必須拒絕，而不是「洗乾淨後放行」
    with pytest.raises(Exception) as parse_error:
        parse_namespace(f"{ACME}/../{BRIGHTPATH}")
    assert "禁用片段" in str(parse_error.value)


def test_integration_cross_tenant_denied_and_split_exact(tmp_path: Path) -> None:
    """整合：跨租戶存取全數被擋 + 分潤分毫不差 + 自主權白名單降級。"""
    config = _load_base_config()
    config["runtime"]["autonomy"] = "supervised_auto"
    config["runtime"]["approved_senders"] = ["ops@acme-ops.example"]
    config["runtime"]["days_in_draft"] = 30
    result = _run(tmp_path, _write_config(tmp_path, config))

    # 1) 隔離：A-02（跨經銷商）、A-03（跨子客戶）、A-04（路徑跳脫）、A-05（缺層級）都必須被拒
    outcomes = {item["id"]: item["outcome"] for item in result["isolation"]["details"]}
    assert outcomes == {
        "A-01": "allowed",
        "A-02": "denied",
        "A-03": "denied",
        "A-04": "denied",
        "A-05": "denied",
        "A-06": "allowed",
    }
    assert result["isolation"]["denied"] == 4

    # 直呼資料層再驗一次：acme 的身分絕對讀不到 brightpath 底下的同名子客戶
    store = TenantStore(data_root=MODULE_DIR / "mock" / "data")
    with pytest.raises(IsolationViolation):
        store.read(
            actor=parse_namespace(f"{ACME}/north-mill"),
            target=parse_namespace(f"{BRIGHTPATH}/north-mill"),
        )
    assert store.denied[0]["reason"] == "經銷商層級不符"

    # 2) 分潤：25% / 75%，每一筆與合計都必須完全相等（不是「近似」）
    acme = _by_slug(result, ACME)
    fees = {item["namespace"]: item for item in acme["sub_clients"]}
    assert fees[f"{ACME}/north-mill"]["reseller_share"] == "375.00"
    assert fees[f"{ACME}/north-mill"]["provider_share"] == "1125.00"
    assert acme["totals"] == {"fee": "3500.00", "reseller": "875.00", "provider": "2625.00"}
    assert result["revenue_totals"] == {
        "fee": "6500.00",
        "reseller": "1625.00",
        "provider": "4875.00",
    }
    policy = SplitPolicy.from_config(config["revenue_share"])
    assert policy.validate() == []
    single = split_fee(Decimal("1500"), policy)
    assert single["reseller"] + single["provider"] == single["fee"]

    # 3) 自主權：命中白名單才自動送出，未命中一律降級為草稿
    assert acme["autonomy"] == "supervised_auto"
    assert acme["delivery"] == "sent"
    assert _by_slug(result, BRIGHTPATH)["autonomy"] == "draft"
    assert _by_slug(result, BRIGHTPATH)["delivery"] == "drafted"

    # 4) 稽核軌跡：每一次拒絕都留下 red 紀錄
    entries = AuditLog.load(tmp_path / "audit.jsonl")
    denials = [item for item in entries if item["event"] == "cross_tenant_denied"]
    assert denials and all(item["severity"] == "red" for item in denials)
    assert result["amber_count"] >= 4
