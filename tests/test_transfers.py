import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock external dependencies if not installed in host python
for mod in ["fastapi", "google", "google.genai", "httpx"]:
    try:
        __import__(mod)
    except (ImportError, AttributeError):
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

from app.account_matching import (
    detect_transfer_type,
    enrich_extracted_data_accounts,
    find_cash_account,
)
from app.csv_processor import parse_csv_transactions
from app.firefly import create_firefly_transaction
from app.models import ReceiptModel
from app.receipt_processing import create_transaction_from_data


def test_receipt_model_transfer_fields():
    print("Testing ReceiptModel with transfer fields...")
    # Default is withdrawal
    r_default = ReceiptModel(
        date="2026-10-28",
        amount=50.0,
        store_name="Store",
        description="Desc",
        category="Groceries",
    )
    assert r_default.transaction_type == "withdrawal"
    assert r_default.destination_account is None

    # Explicit transfer
    r_transfer = ReceiptModel(
        date="2026-10-28",
        amount=100.0,
        store_name="ATM",
        description="ATM cash withdrawal",
        category="Other",
        transaction_type="transfer",
        destination_account="Cash wallet",
    )
    assert r_transfer.transaction_type == "transfer"
    assert r_transfer.destination_account == "Cash wallet"
    print("ReceiptModel transfer fields tests passed!")


def test_detect_transfer_type():
    print("Testing detect_transfer_type and find_cash_account...")
    asset_accounts = ["Easybank Giro", "Cash wallet", "Sparkonto"]

    # 1. find_cash_account
    assert find_cash_account(asset_accounts) == "Cash wallet"
    assert find_cash_account(["Checking", "Savings", "Bargeld"]) == "Bargeld"

    # 2. ATM cash withdrawal detection
    tx_type, dest, reason = detect_transfer_type(
        store_name="BANKOMAT FILIALE",
        description="Bargeldauszahlung mit Karte",
        source_account="Easybank Giro",
        asset_accounts=asset_accounts,
    )
    assert tx_type == "transfer"
    assert dest == "Cash wallet"
    assert "ATM cash withdrawal" in reason

    # 3. Internal transfer to another asset account (Sparkonto)
    tx_type2, dest2, reason2 = detect_transfer_type(
        store_name="Übertrag auf Sparkonto",
        description="Monatliche Sparrate",
        source_account="Easybank Giro",
        asset_accounts=asset_accounts,
    )
    assert tx_type2 == "transfer"
    assert dest2 == "Sparkonto"
    assert "Sparkonto" in reason2

    # 4. Normal purchase (should remain withdrawal)
    tx_type3, dest3, reason3 = detect_transfer_type(
        store_name="BILLA Plus",
        description="Groceries",
        source_account="Easybank Giro",
        asset_accounts=asset_accounts,
    )
    assert tx_type3 == "withdrawal"
    assert dest3 is None

    print("detect_transfer_type tests passed!")


def test_enrich_extracted_data_accounts_transfer():
    print("Testing enrich_extracted_data_accounts auto-detects transfers...")
    asset_accounts = ["Easybank Giro", "Cash wallet", "Sparkonto"]

    # Test ATM transaction
    extracted_atm = {
        "date": "2026-10-28",
        "amount": 100.0,
        "store_name": "Bankomat",
        "description": "Bargeldbehebung",
        "category": "Other",
        "card_last_four": "1234",
    }
    mock_settings = MagicMock()
    mock_settings.account_card_mappings = "1234: Easybank Giro"

    with patch("app.account_matching.get_settings", return_value=mock_settings):
        enriched = enrich_extracted_data_accounts(
            extracted_atm, asset_accounts, fallback_account="Easybank Giro"
        )
        assert enriched["transaction_type"] == "transfer"
        assert enriched["source_account"] == "Easybank Giro"
        assert enriched["destination_account"] == "Cash wallet"
        assert "ATM" in enriched["account_match_reason"]

    print("enrich_extracted_data_accounts transfer tests passed!")


def test_create_firefly_transaction_transfer():
    print("Testing create_firefly_transaction transfer payload...")
    mock_settings = MagicMock()
    mock_settings.firefly_iii_url = "http://firefly.test"
    mock_settings.firefly_iii_token = "secret"
    mock_settings.firefly_api_url = "http://firefly.test/api/v1/"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": {"id": "999"}}

    receipt_transfer = ReceiptModel(
        date="2026-10-28",
        time="14:30",
        amount=100.0,
        store_name="ATM",
        description="Cash withdrawal",
        category="General",
        budget="Monthly",  # should be ignored for transfers
        transaction_type="transfer",
        destination_account="Cash wallet",
    )

    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.post", return_value=mock_resp
    ) as mock_post:
        res = create_firefly_transaction(
            receipt_transfer,
            source_account="Easybank Giro",
            destination_account="Cash wallet",
            transaction_type="transfer",
        )
        assert res["data"]["id"] == "999"
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]["transactions"][0]

        # Verify transfer fields
        assert payload["type"] == "transfer"
        assert payload["source_name"] == "Easybank Giro"
        assert payload["destination_name"] == "Cash wallet"
        assert payload["amount"] == "100.0"
        assert payload["budget_name"] is None, "Transfers in Firefly III cannot have budgets"
        assert "transfer" in payload["tags"]

        # Test error when source == destination
        try:
            create_firefly_transaction(
                receipt_transfer,
                source_account="Cash wallet",
                destination_account="Cash wallet",
                transaction_type="transfer",
            )
            assert False, "Should raise ValueError when source == destination"
        except ValueError as e:
            assert "cannot be the same" in str(e)

    print("create_firefly_transaction transfer tests passed!")


def test_csv_ingestion_with_transfers():
    print("Testing CSV processor with ATM withdrawals and account transfers...")
    csv_content = """Date,Store,Description,Amount,Card
2026-10-27,BILLA PLUS,Wocheneinkauf,45.20,1234
2026-10-28,BANKOMAT 1234,Bargeldauszahlung,100.00,1234
2026-10-29,UEBERTRAG SPARKONTO,Monatliches Sparen,200.00,1234
"""
    asset_accounts = ["Easybank Giro", "Cash wallet", "Sparkonto"]
    mock_settings = MagicMock()
    mock_settings.account_card_mappings = "1234: Easybank Giro"

    with patch("app.account_matching.get_settings", return_value=mock_settings):
        txs = parse_csv_transactions(
            csv_content,
            default_account="Easybank Giro",
            asset_accounts=asset_accounts,
        )

        assert len(txs) == 3

        # Row 1: Billa (withdrawal)
        assert txs[0]["transaction_type"] == "withdrawal"
        assert txs[0]["store_name"] == "BILLA PLUS"
        assert txs[0]["source_account"] == "Easybank Giro"

        # Row 2: ATM cash withdrawal (transfer to Cash wallet)
        assert txs[1]["transaction_type"] == "transfer"
        assert txs[1]["source_account"] == "Easybank Giro"
        assert txs[1]["destination_account"] == "Cash wallet"
        assert txs[1]["amount"] == 100.00

        # Row 3: Transfer to Sparkonto (transfer between asset accounts)
        assert txs[2]["transaction_type"] == "transfer"
        assert txs[2]["source_account"] == "Easybank Giro"
        assert txs[2]["destination_account"] == "Sparkonto"
        assert txs[2]["amount"] == 200.00

    print("CSV processor transfer tests passed!")


if __name__ == "__main__":
    test_receipt_model_transfer_fields()
    test_detect_transfer_type()
    test_enrich_extracted_data_accounts_transfer()
    test_create_firefly_transaction_transfer()
    test_csv_ingestion_with_transfers()
    print("\nAll transfer unit tests passed successfully!")
