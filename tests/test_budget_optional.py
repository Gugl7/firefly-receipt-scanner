import json
import os
import sys
from unittest.mock import MagicMock, patch

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


def test_receipt_model_optional_budget():
    print("Testing ReceiptModel with optional / None budget...")
    # 1. Budget explicitly None
    r1 = ReceiptModel(
        date="2026-10-28",
        amount=25.0,
        store_name="Store",
        description="Item",
        category="Groceries",
        budget=None,
    )
    assert r1.budget is None

    # 2. Budget omitted (defaults to None)
    r2 = ReceiptModel(
        date="2026-10-28",
        amount=25.0,
        store_name="Store",
        description="Item",
        category="Groceries",
    )
    assert r2.budget is None

    # 3. Budget with string value
    r3 = ReceiptModel(
        date="2026-10-28",
        amount=25.0,
        store_name="Store",
        description="Item",
        category="Groceries",
        budget="Monthly",
    )
    assert r3.budget == "Monthly"

    # 4. JSON parsing with null budget
    raw_json = '{"date": "2026-10-28", "amount": 25.0, "store_name": "Store", "description": "Item", "category": "Groceries", "budget": null}'
    r4 = validate_model_json(ReceiptModel, raw_json)
    assert r4.budget is None

    print("ReceiptModel optional budget tests passed!")


def test_create_firefly_transaction_budget_handling():
    print("Testing create_firefly_transaction budget_name payload formatting...")
    mock_settings = MagicMock()
    mock_settings.firefly_iii_url = "http://firefly.test"
    mock_settings.firefly_iii_token = "secret"
    mock_settings.firefly_api_url = "http://firefly.test/api/v1/"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": {"id": "123"}}

    # Case A: Receipt with explicit budget
    r_budget = ReceiptModel(
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
        create_firefly_transaction(r_budget, "Checking")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["transactions"][0]["budget_name"] == "Weekly Groceries"

    # Case B: Receipt with budget=None
    r_none = ReceiptModel(
        date="2026-10-28",
        amount=15.0,
        store_name="Billa",
        description="Snacks",
        category="Groceries",
        budget=None,
    )
    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.post", return_value=mock_resp
    ) as mock_post:
        create_firefly_transaction(r_none, "Checking")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["transactions"][0]["budget_name"] is None

    # Case C: Receipt with budget="None" (string) or empty string ""
    for budget_str in ["None", "none", "null", ""]:
        r_str = ReceiptModel(
            date="2026-10-28",
            amount=15.0,
            store_name="Billa",
            description="Snacks",
            category="Groceries",
            budget=budget_str,
        )
        with patch("app.firefly.get_settings", return_value=mock_settings), patch(
            "app.firefly.requests.post", return_value=mock_resp
        ) as mock_post:
            create_firefly_transaction(r_str, "Checking")
            _, kwargs = mock_post.call_args
            payload = kwargs["json"]
            assert (
                payload["transactions"][0]["budget_name"] is None
            ), f"Expected None for budget '{budget_str}', got {payload['transactions'][0]['budget_name']}"

    print("create_firefly_transaction budget_name tests passed!")


def test_csv_without_budget_column():
    print("Testing CSV processor when no budget is present...")
    csv_data = "Date,Description,Amount\n2026-10-28,Billa,32.45\n"
    txs = parse_csv_transactions(
        csv_data,
        default_account="Cash wallet",
        asset_accounts=["Cash wallet"],
        budgets=["Monthly", "Weekly"],
    )
    assert len(txs) == 1
    # When no budget column exists, default to empty string ("no budget")
    assert txs[0]["budget"] == ""
    assert "available_budgets" in txs[0]
    print("CSV processor optional budget tests passed!")


if __name__ == "__main__":
    test_receipt_model_optional_budget()
    test_create_firefly_transaction_budget_handling()
    test_csv_without_budget_column()
    print("\nAll optional budget tests passed successfully!")

