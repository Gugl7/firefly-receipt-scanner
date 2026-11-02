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

from app.csv_processor import parse_csv_transactions
from app.firefly import create_firefly_transaction
from app.models import ReceiptModel, validate_model_json, validate_model_obj
from app.receipt_processing import create_transaction_from_data


def test_receipt_model_optional_category():
    print("Testing ReceiptModel with optional / None category...")
    # 1. Category explicitly None
    r1 = ReceiptModel(
        date="2026-10-28",
        amount=25.0,
        store_name="Store",
        description="Item",
        category=None,
    )
    assert r1.category is None

    # 2. Category omitted (defaults to None)
    r2 = ReceiptModel(
        date="2026-10-28",
        amount=25.0,
        store_name="Store",
        description="Item",
    )
    assert r2.category is None

    # 3. Category with string value
    r3 = ReceiptModel(
        date="2026-10-28",
        amount=25.0,
        store_name="Store",
        description="Item",
        category="Groceries",
    )
    assert r3.category == "Groceries"

    # 4. JSON parsing with null category
    raw_json = '{"date": "2026-10-28", "amount": 25.0, "store_name": "Store", "description": "Item", "category": null}'
    r4 = validate_model_json(ReceiptModel, raw_json)
    assert r4.category is None

    print("ReceiptModel optional category tests passed!")


def test_create_firefly_transaction_category_handling():
    print("Testing create_firefly_transaction category_name payload formatting...")
    mock_settings = MagicMock()
    mock_settings.firefly_iii_url = "http://firefly.test"
    mock_settings.firefly_iii_token = "secret"
    mock_settings.firefly_api_url = "http://firefly.test/api/v1/"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": {"id": "123"}}

    # Case A: Receipt with explicit category
    r_cat = ReceiptModel(
        date="2026-10-28",
        amount=25.0,
        store_name="Billa",
        description="Groceries",
        category="Groceries",
        budget="Weekly Groceries",
    )
    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.post", return_value=mock_resp
    ) as mock_post:
        create_firefly_transaction(r_cat, "Checking")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["transactions"][0]["category_name"] == "Groceries"

    # Case B: Receipt with category=None
    r_none = ReceiptModel(
        date="2026-10-28",
        amount=15.0,
        store_name="Billa",
        description="Snacks",
        category=None,
    )
    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.post", return_value=mock_resp
    ) as mock_post:
        create_firefly_transaction(r_none, "Checking")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["transactions"][0]["category_name"] is None

    # Case C: Receipt with category="None" (string), "none", "null", or empty string ""
    for cat_str in ["None", "none", "null", "", "   "]:
        r_str = ReceiptModel(
            date="2026-10-28",
            amount=15.0,
            store_name="Billa",
            description="Snacks",
            category=cat_str,
        )
        with patch("app.firefly.get_settings", return_value=mock_settings), patch(
            "app.firefly.requests.post", return_value=mock_resp
        ) as mock_post:
            create_firefly_transaction(r_str, "Checking")
            _, kwargs = mock_post.call_args
            payload = kwargs["json"]
            assert (
                payload["transactions"][0]["category_name"] is None
            ), f"Expected None for category '{cat_str}', got {payload['transactions'][0]['category_name']}"

    # Case D: Transfer and deposit transactions with category=None
    r_transfer = ReceiptModel(
        date="2026-10-28",
        amount=50.0,
        store_name="Cash wallet",
        description="ATM withdrawal",
        category=None,
        transaction_type="transfer",
        destination_account="Cash wallet",
    )
    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.post", return_value=mock_resp
    ) as mock_post:
        create_firefly_transaction(r_transfer, "Easybank Giro")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["transactions"][0]["category_name"] is None

    r_deposit = ReceiptModel(
        date="2026-10-28",
        amount=50.0,
        store_name="Employer",
        description="Salary bonus",
        category=None,
        transaction_type="deposit",
    )
    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.post", return_value=mock_resp
    ) as mock_post:
        create_firefly_transaction(r_deposit, "Easybank Giro")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["transactions"][0]["category_name"] is None

    print("create_firefly_transaction category_name tests passed!")


def test_create_transaction_from_data_optional_category():
    print("Testing create_transaction_from_data with empty/None category...")
    import asyncio

    mock_settings = MagicMock()
    mock_settings.firefly_iii_url = "http://firefly.test"
    mock_settings.firefly_iii_token = "secret"
    mock_settings.firefly_api_url = "http://firefly.test/api/v1/"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": {
            "id": "456",
            "attributes": {"transactions": [{"transaction_journal_id": 999}]},
        }
    }

    # Dict payload from review form with category = ""
    payload = {
        "date": "2026-10-28",
        "time": "14:35",
        "amount": 25.5,
        "store_name": "Store",
        "description": "Item",
        "category": "",
        "budget": "",
        "transaction_type": "withdrawal",
        "destination_account": "",
    }

    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.post", return_value=mock_resp
    ) as mock_post:
        result = asyncio.run(
            create_transaction_from_data(payload, "Checking")
        )
        _, kwargs = mock_post.call_args
        sent_tx = kwargs["json"]["transactions"][0]
        assert sent_tx["category_name"] is None
        assert sent_tx["budget_name"] is None

    print("create_transaction_from_data optional category tests passed!")


def test_csv_processor_optional_category():
    print("Testing CSV processor with explicit empty / None category...")
    csv_data = """Date,Store,Amount,Category,Description
2026-10-25,Store A,10.00,Groceries,Has category
2026-10-26,Store B,20.00,,Empty category
2026-10-27,Store C,30.00,None,None string category
2026-10-28,Store D,40.00,null,null string category
"""
    txs = parse_csv_transactions(
        csv_data,
        default_account="Checking",
        asset_accounts=["Checking"],
        categories=["Groceries", "Dining", "Shopping"],
    )
    assert len(txs) == 4
    assert txs[0]["category"] == "Groceries"
    assert txs[1]["category"] == ""
    assert txs[2]["category"] == ""
    assert txs[3]["category"] == ""
    print("CSV processor optional category tests passed!")


if __name__ == "__main__":
    test_receipt_model_optional_category()
    test_create_firefly_transaction_category_handling()
    test_create_transaction_from_data_optional_category()
    test_csv_processor_optional_category()
    print("\nAll optional category tests passed successfully!")

