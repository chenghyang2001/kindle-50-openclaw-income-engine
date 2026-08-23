"""demo14 測試（3 個：happy / edge / integration）。

一律離線可跑：不呼叫任何真實 API，去重狀態檔一律指向 pytest 的 tmp_path，
避免污染模組目錄下的 .handled.json。

臨時設定檔以 JSON 寫出——YAML 是 JSON 的超集，直接 dump JSON 就是合法 YAML，
測試因此不必自己依賴 pyyaml。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import triage  # noqa: E402


def _load_main() -> Any:
    """以唯一模組名載入 main.py，避免整個 repo 一起跑 pytest 時與其他 demo 的 main 撞名。"""
    spec = importlib.util.spec_from_file_location("demo14_main", MODULE_DIR / "main.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {MODULE_DIR / 'main.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo14_main"] = module
    spec.loader.exec_module(module)
    return module


demo14 = _load_main()

AI_DISCLOSURE = "（我是 Brightleaf Living 的 AI 客服助理 Nova，需要真人協助請回覆「真人」，我會立刻轉接。）"


def _run(tmp_path: Path, extra: list[str] | None = None) -> dict[str, Any]:
    """跑一次 mock 流程，狀態檔隔離在 tmp_path。"""
    argv = ["--mock", "--notify", "console", "--state-file", str(tmp_path / ".handled.json")]
    args = demo14.build_parser().parse_args(argv + (extra or []))
    return demo14.run(args)


def _base_config(channels: list[dict[str, Any]]) -> dict[str, Any]:
    """組出一份與 config.yaml 等價的最小設定，供測試改寫特定欄位。

    knowledge_base / orders 仍指向真實的 mock 檔（相對路徑會被接到模組目錄底下），
    這樣測試驗的是真的那份知識庫，而不是另一份為了讓測試通過而寫的假資料。
    """
    return {
        "module": {"id": "14", "name": "多渠道客服分流"},
        "runtime": {"autonomy": "draft", "approved_senders": [], "days_in_draft": 0},
        "safety": {"refund_dispute_do_not_process": True, "never_claim_human": True},
        "persona": {
            "agent_name": "Nova",
            "greeting": "{NAME} 您好，這裡是 Brightleaf Living 客服：",
            "ai_disclosure": AI_DISCLOSURE,
            "human_impersonation_phrases": ["我是真人", "我不是機器人", "真人客服為您服務"],
            "holding_response_template": "{NAME} 您好，我們已收到您關於退款／款項爭議的來訊，並已轉交專責同事處理。",
        },
        "triage": {
            "response_target_minutes": 2,
            "reply_max_words": 100,
            "fetch_delay_seconds": 1.0,
            "order_id_pattern": r"\b(BL-\d{5})\b",
            "refund_amount_threshold": 3000,
            "vip_contacts": ["vip.lin@brightleaf-partners.com"],
            "signals": {
                "refund_dispute": ["退款", "退貨", "爭議款", "拒付", "refund"],
                "negative_vip_legal": ["律師", "消保官", "投訴", "太誇張"],
                "order_status": ["到哪", "出貨", "物流", "什麼時候到"],
            },
        },
        "knowledge_base": {"file": "mock/knowledge_base.json", "match_min_score": 2},
        "orders": {"mock_file": "mock/orders.json"},
        "channels": channels,
    }


def _write_config(tmp_path: Path, config: dict[str, Any]) -> str:
    """把設定寫成臨時檔並回傳路徑字串。"""
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ── 1. Happy path ───────────────────────────────────────────────────────────
def test_happy_path(tmp_path: Path) -> None:
    """標準 mock 輸入（4 個渠道 11 則）-> run() 回傳預期結構與分流結果。"""
    result = _run(tmp_path)

    assert result["module_id"] == "14"
    assert result["mode"] == "mock"
    assert result["dry_run"] is False
    assert result["fetch_errors"] == []
    assert result["channels_configured"] == 4
    assert result["response_target_minutes"] == 2
    # 書中規格：知識庫 30-50 條，實際 33 條，因此不該有知識庫相關的琥珀警示
    assert 30 <= result["knowledge_base_entries"] <= 50

    # 11 則進件：6 則可自動回覆（3 FAQ + 3 訂單查詢）、5 則升級真人
    assert result["fetched"] == 11
    assert result["new"] == 11
    assert result["skipped_duplicates"] == 0
    assert len(result["auto_replied"]) == 6
    assert len(result["escalated"]) == 5
    assert len(result["high_priority"]) == 3

    categories = sorted(item["category"] for item in result["auto_replied"])
    assert categories == ["faq", "faq", "faq", "order_status", "order_status", "order_status"]
    # 自動回覆的每一個字都必須有出處（知識庫條目或訂單編號），不得是模型生成
    assert all(item["source"].startswith(("knowledge_base:", "orders:")) for item in result["auto_replied"])
    # 預設 draft：草稿照樣產出，但一則都不會被送出
    assert result["autonomy_level"] == "draft"
    assert all(item["can_send"] is False for item in result["auto_replied"])
    assert result["notified"]["replies_sent"] == 0
    assert result["notified"]["replies_drafted"] == 6
    # 升級卡片不受自主權限制：值班人員永遠收得到（否則整個模組的價值歸零）
    assert result["notified"]["escalations_sent"] == 5
    assert Path(result["state_file"]).is_file()


# ── 2. Edge case ────────────────────────────────────────────────────────────
def _write_edge_channel(tmp_path: Path) -> Path:
    """寫一份含空訊息與特殊字元的臨時渠道檔。"""
    messages = [
        {"message_id": "e-000", "customer_name": "空白測試", "customer_contact": "blank@example.com", "subject": "", "text": ""},
        {
            "message_id": "e-001",
            "customer_name": "符號測試",
            "customer_contact": "emoji@example.com",
            "subject": "🙃《【緊急】》",
            "text": "商品破損\t\t我要退款！！！\n\n<script>alert(1)</script> 金額 NT$1,200",
        },
        {
            "message_id": "e-002",
            "customer_name": "蔡曼寧",
            "customer_contact": "manning.tsai@example.com",
            "subject": "",
            "text": "請問運費怎麼算？要滿多少才免運？",
        },
    ]
    path = tmp_path / "messages_edge.json"
    path.write_text(json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8")
    return path


def test_edge_case_blank_special_chars_and_broken_channel(tmp_path: Path) -> None:
    """邊界：空訊息、特殊字元、單一渠道抓取失敗、第二次執行全部去重。"""
    edge_path = _write_edge_channel(tmp_path)
    config = _base_config(
        [
            # 故意指向不存在的檔案：驗證單一渠道失敗不會中斷其他渠道
            {"id": "instagram", "enabled": True, "mock_file": "mock/does_not_exist.json"},
            {"id": "gmail", "enabled": True, "mock_file": str(edge_path)},
            # 停用的渠道完全不該被抓取，也不該產生錯誤
            {"id": "whatsapp", "enabled": False, "mock_file": "mock/does_not_exist_either.json"},
        ]
    )
    config_path = _write_config(tmp_path, config)

    first = _run(tmp_path, ["--config", config_path])

    # 單一渠道失敗只留下一則錯誤，另一個渠道的 3 則訊息照常處理完
    assert len(first["fetch_errors"]) == 1
    assert "does_not_exist.json" in first["fetch_errors"][0]
    assert "does_not_exist_either" not in " ".join(first["fetch_errors"])
    assert first["fetched"] == 3
    assert first["amber_count"] >= 1

    by_key = {item["key"]: item for item in first["escalated"]}
    # 空訊息：不猜、不自動回覆，一律交給真人
    assert by_key["gmail:e-000"]["category"] == triage.CATEGORY_UNKNOWN
    # 特殊字元 + 標籤注入不影響分類；1,200 未達 3,000 門檻故為 NORMAL
    special = by_key["gmail:e-001"]
    assert special["category"] == triage.CATEGORY_REFUND_DISPUTE
    assert special["refund_amount"] == 1200.0
    assert special["priority"] == triage.PRIORITY_NORMAL
    assert special["holding_response"].endswith(AI_DISCLOSURE)
    # 三則裡只有 FAQ 那則可自動回覆
    assert [item["key"] for item in first["auto_replied"]] == ["gmail:e-002"]

    # 第二次執行：同一份資料全部命中去重，不會重複打擾客戶，也不會重複轟炸值班頻道
    second = _run(tmp_path, ["--config", config_path])
    assert second["fetched"] == 3
    assert second["new"] == 0
    assert second["skipped_duplicates"] == 3
    assert second["auto_replied"] == []
    assert second["escalated"] == []


# ── 3. Integration ──────────────────────────────────────────────────────────
def test_integration_hard_rules_and_autonomy(tmp_path: Path) -> None:
    """整合：安全鐵律強制覆寫 + 自主權放行白名單 + 退款爭議絕不自動回覆。"""
    config = _base_config(
        [
            {"id": "gmail", "enabled": True, "mock_file": "mock/messages_gmail.json"},
            {"id": "whatsapp", "enabled": True, "mock_file": "mock/messages_whatsapp.json"},
        ]
    )
    # 兩條鐵律都被關掉、自主權開到最大：這是最壞的設定組合
    config["safety"] = {"refund_dispute_do_not_process": False, "never_claim_human": False}
    config["runtime"] = {
        "autonomy": "supervised_auto",
        "approved_senders": ["@example.com", "+886912345678"],
        "days_in_draft": 30,
    }
    result = _run(tmp_path, ["--config", _write_config(tmp_path, config)])

    # (1) 安全鐵律：config 關不掉，被強制覆寫回 true 並留下警告
    assert result["refund_dispute_do_not_process"] is True
    assert result["never_claim_human"] is True
    assert sum("強制覆寫為 true" in text for text in result["warnings"]) == 2
    assert result["amber_count"] >= 2

    # (2) 退款爭議一律不自動回覆——即使自主權開到 supervised_auto 且寄件人在白名單內
    refund_items = [item for item in result["escalated"] if item["category"] == triage.CATEGORY_REFUND_DISPUTE]
    assert len(refund_items) == 2
    assert all(item["category"] != triage.CATEGORY_REFUND_DISPUTE for item in result["auto_replied"])
    refund_keys = {item["key"] for item in refund_items}
    assert refund_keys.isdisjoint({item["key"] for item in result["auto_replied"]})
    # 每一則退款爭議都必須帶 Holding Response（只確認收到，不談金額與責任）
    assert all(item["holding_response"] for item in refund_items)
    # Holding Response 不得出現任何處理結果、責任歸屬或金額——那些話只有真人能講
    forbidden_in_holding = ("已核准", "可以退", "不能退", "無法退", "全額", "NT$", "責任")
    assert all(
        not any(word in item["holding_response"] for word in forbidden_in_holding)
        for item in refund_items
    )
    assert all(item["holding_response"].endswith(AI_DISCLOSURE) for item in refund_items)

    # (3) Slack 升級卡片格式逐字照 apxF_p12
    legal_case = next(item for item in refund_items if item["key"] == "gmail:gm-20260824-003")
    assert legal_case["priority"] == triage.PRIORITY_HIGH  # 命中律師字眼 + 金額 48,000 超過門檻
    assert legal_case["refund_amount"] == 48000.0
    assert legal_case["payload"].startswith("*[HIGH] Support Escalation - GMAIL - REFUND_DISPUTE*\n> Customer: 許鎮宇 | Query: ")

    # (4) 自主權：白名單命中才真的送出，且每則自動回覆都揭露 AI 身分
    assert result["autonomy_level"] == "supervised_auto"
    assert result["auto_replied"], "白名單情境下應至少有一則自動回覆"
    assert all(item["can_send"] is True for item in result["auto_replied"])
    assert result["notified"]["replies_sent"] == len(result["auto_replied"])
    assert all(item["reply"].endswith(AI_DISCLOSURE) for item in result["auto_replied"])
    assert all(not triage.find_impersonation(item["reply"], ["我是真人", "我不是機器人", "真人客服為您服務"])
               for item in result["auto_replied"])
