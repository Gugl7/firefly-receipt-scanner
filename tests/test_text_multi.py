import asyncio
import json
import os
import shutil
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock external dependencies if not installed in host python
for mod in ["fastapi", "google", "google.genai", "httpx"]:
    try:
        __import__(mod)
    except (ImportError, AttributeError):
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

from app.models import (
    MultiReceiptModel,
    PaymentSplit,
    ReceiptModel,
    validate_model_json,
    validate_model_obj,
)
from app.receipt_processing import (
    build_extraction_prompt,
    extract_json_str,
    extract_receipt_data_from_text,
    parse_multi_receipt_json,
    parse_receipt_json,
)


def test_models_and_validators():
    print("Testing MultiReceiptModel and validate helpers...")
    # Single receipt
    r1 = ReceiptModel(
        date="2026-10-25",
        time="14:30",
        amount=42.50,
        store_name="Billa Plus",
        description="Groceries",
        category="Groceries",
        budget="Monthly",
        payment_method="card",
        card_last_four="1234",
    )
    r2 = ReceiptModel(
        date="2026-10-26",
        time="08:15",
        amount="€ 65,00",
        store_name="Shell",
        description="Gasoline",
        category="Transportation",
        budget="Monthly",
        payment_method="card",
        card_last_four="5678",
    )

    multi = MultiReceiptModel(transactions=[r1, r2])
    assert len(multi.transactions) == 2
    assert multi.transactions[0].store_name == "Billa Plus"
    assert multi.transactions[1].amount == 65.0

    # Test validate_model_json
    raw_multi_json = json.dumps({
        "transactions": [
            {
                "date": "2026-10-25",
                "time": "12:00",
                "amount": 15.20,
                "store_name": "Bakery",
                "description": "Bread",
                "category": "Groceries",
                "budget": "Monthly",
            }
        ]
    })
    parsed_multi = validate_model_json(MultiReceiptModel, raw_multi_json)
    assert len(parsed_multi.transactions) == 1
    assert parsed_multi.transactions[0].store_name == "Bakery"

    # Test validate_model_obj
    obj_multi = validate_model_obj(ReceiptModel, {
        "date": "2026-10-27",
        "amount": "100.50",
        "store_name": "MediaMarkt",
        "description": "Cable",
        "category": "Shopping",
        "budget": "Monthly",
    })
    assert obj_multi.amount == 100.50

    # Test None / null resilience for store_name, description, amount, date
    null_rx = validate_model_obj(ReceiptModel, {
        "date": None,
        "amount": None,
        "store_name": None,
        "description": None,
    })
    assert null_rx.store_name == "Unknown Store"
    assert null_rx.description == "Receipt"
    assert null_rx.amount == 0.0
    assert len(null_rx.date) == 10
    print("MultiReceiptModel and validator tests passed!")


def test_parse_multi_receipt_json():
    print("Testing parse_multi_receipt_json with various formats...")

    # 1. Standard format: {"transactions": [...]}
    content_trans = json.dumps({
        "transactions": [
            {
                "date": "2026-10-25",
                "time": "14:30",
                "amount": 42.50,
                "store_name": "Billa",
                "description": "Groceries",
                "category": "Groceries",
                "budget": "Monthly",
            },
            {
                "date": "2026-10-26",
                "time": "09:00",
                "amount": 5.80,
                "store_name": "Espresso Bar",
                "description": "Coffee",
                "category": "Dining",
                "budget": "Monthly",
            },
        ]
    })
    res1 = parse_multi_receipt_json(content_trans)
    assert len(res1.transactions) == 2
    assert res1.transactions[0].store_name == "Billa"
    assert res1.transactions[1].store_name == "Espresso Bar"

    # 2. Markdown codeblock wrapped
    content_md = f"```json\n{content_trans}\n```"
    res2 = parse_multi_receipt_json(content_md)
    assert len(res2.transactions) == 2
    assert res2.transactions[0].amount == 42.50

    # 3. Alternative key: {"receipts": [...]}
    content_receipts = json.dumps({
        "receipts": [
            {
                "date": "2026-10-25",
                "amount": 12.0,
                "store_name": "Pharmacy",
                "description": "Meds",
                "category": "Other",
                "budget": "Monthly",
            }
        ]
    })
    res3 = parse_multi_receipt_json(content_receipts)
    assert len(res3.transactions) == 1
    assert res3.transactions[0].store_name == "Pharmacy"

    # 4. Top-level array: [ {...}, {...} ]
    content_arr = json.dumps([
        {
            "date": "2026-10-25",
            "amount": 30.0,
            "store_name": "Store A",
            "description": "Item A",
            "category": "Shopping",
            "budget": "Monthly",
        },
        {
            "date": "2026-10-26",
            "amount": 40.0,
            "store_name": "Store B",
            "description": "Item B",
            "category": "Shopping",
            "budget": "Monthly",
        },
    ])
    res4 = parse_multi_receipt_json(content_arr)
    assert len(res4.transactions) == 2
    assert res4.transactions[1].store_name == "Store B"

    # 5. Single receipt object (fallback to 1 transaction)
    content_single = json.dumps({
        "date": "2026-10-25",
        "time": "18:45",
        "amount": 19.99,
        "store_name": "Pizzeria",
        "description": "Dinner",
        "category": "Dining",
        "budget": "Monthly",
    })
    res5 = parse_multi_receipt_json(content_single)
    assert len(res5.transactions) == 1
    assert res5.transactions[0].store_name == "Pizzeria"
    assert res5.transactions[0].time == "18:45"

    # 6. Null store_name or null fields from LLM
    content_null = json.dumps({
        "transactions": [
            {
                "date": "2026-11-06",
                "amount": 25.50,
                "store_name": None,
                "description": None,
            }
        ]
    })
    res6 = parse_multi_receipt_json(content_null)
    assert len(res6.transactions) == 1
    assert res6.transactions[0].store_name == "Unknown Store"
    assert res6.transactions[0].description == "Receipt"
    assert res6.transactions[0].amount == 25.50

    print("parse_multi_receipt_json tests passed!")


def test_build_extraction_prompt_multi():
    print("Testing build_extraction_prompt with text vs image...")
    categories = ["Groceries", "Dining", "Shopping"]
    budgets = ["Monthly", "Other"]

    # Image prompt (should NOT have "transactions" array wrapper in schema)
    img_prompt = build_extraction_prompt(categories, budgets, receipt_text=None)
    assert "attached receipt image" in img_prompt
    assert '"date": "YYYY-MM-DD"' in img_prompt
    assert '"transactions": [' not in img_prompt

    # Text prompt (should have "transactions" array schema and multi-instructions)
    pasted_text = "1) Billa 15.20 EUR Groceries\n2) Shell 50.00 EUR Fuel"
    text_prompt = build_extraction_prompt(categories, budgets, receipt_text=pasted_text)
    assert "Billa 15.20 EUR Groceries" in text_prompt
    assert '"transactions": [' in text_prompt
    assert "MULTIPLE distinct transactions" in text_prompt
    print("build_extraction_prompt tests passed!")


def test_extract_receipt_data_from_text():
    print("Testing extract_receipt_data_from_text with mock LLM...")

    mock_llm_result = MultiReceiptModel(
        transactions=[
            ReceiptModel(
                date="2026-10-25 14:30",
                amount=24.50,
                store_name="Billa Plus",
                description="Weekly Snacks",
                category="Groceries",
                budget="Monthly",
                payment_method="card",
                card_last_four="PAN: *******1234",
            ),
            ReceiptModel(
                date="2026-10-26",
                time="19:15",
                amount=55.00,
                store_name="Trattoria",
                description="Split dinner",
                category="Dining",
                budget="Monthly",
                payment_method="split",
                is_split_payment=True,
                splits=[
                    PaymentSplit(payment_method="cash", amount=20.0),
                    PaymentSplit(payment_method="card", amount=35.0, card_last_four="5678"),
                ],
            ),
        ]
    )

    with patch(
        "app.receipt_processing._run_llm_extraction",
        new=AsyncMock(return_value=mock_llm_result),
    ), patch(
        "app.receipt_processing.get_firefly_categories",
        return_value=["Groceries", "Dining"],
    ), patch(
        "app.receipt_processing.get_firefly_budgets",
        return_value=["Monthly"],
    ):
        pasted = "Billa receipt and restaurant receipt..."
        txs = asyncio.run(extract_receipt_data_from_text(pasted))

        assert len(txs) == 2
        # First transaction checks
        assert txs[0]["store_name"] == "Billa Plus"
        assert txs[0]["date"] == "2026-10-25"
        assert txs[0]["time"] == "14:30"
        assert txs[0]["amount"] == 24.50
        assert txs[0]["card_last_four"] == "1234"
        assert txs[0]["is_split_payment"] is False

        # Second transaction checks (split payment)
        assert txs[1]["store_name"] == "Trattoria"
        assert txs[1]["date"] == "2026-10-26"
        assert txs[1]["time"] == "19:15"
        assert txs[1]["amount"] == 55.00
        assert txs[1]["is_split_payment"] is True
        assert len(txs[1]["splits"]) == 2
        assert txs[1]["splits"][0]["amount"] == 20.0
        assert txs[1]["splits"][0]["payment_method"] == "cash"
        assert txs[1]["splits"][1]["amount"] == 35.0
        assert txs[1]["splits"][1]["card_last_four"] == "5678"

    print("extract_receipt_data_from_text tests passed!")


def test_batch_lifecycle_simulation():
    print("Testing multi-transaction batch session creation & navigation...")

    # Simulate extracted multi transactions
    raw_txs = [
        {
            "date": "2026-10-25",
            "time": "10:00",
            "amount": 12.50,
            "store_name": "Billa",
            "description": "Snacks",
            "category": "Groceries",
            "budget": "Monthly",
            "card_last_four": "1234",
            "source_account": "Easybank Giro",
            "splits": [],
        },
        {
            "date": "2026-10-26",
            "time": "15:00",
            "amount": 45.00,
            "store_name": "OMV Gas",
            "description": "Fuel",
            "category": "Transportation",
            "budget": "Monthly",
            "card_last_four": "",
            "source_account": "Cash wallet",
            "splits": [],
        },
        {
            "date": "2026-10-27",
            "time": "19:30",
            "amount": 32.00,
            "store_name": "Pizzeria",
            "description": "Pizza dinner",
            "category": "Dining",
            "budget": "Monthly",
            "card_last_four": "1234",
            "source_account": "Easybank Giro",
            "splits": [],
        },
    ]

    batch_id = "test-batch-12345"
    batch_dir = os.path.join(tempfile.gettempdir(), "receipt_scanner", batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    try:
        # Write batch files as app.py does
        with open(os.path.join(batch_dir, "batch_type.txt"), "w") as f_type:
            f_type.write("text")

        for idx, tx in enumerate(raw_txs):
            target_json = os.path.join(batch_dir, f"{idx}.json")
            with open(target_json, "w", encoding="utf-8") as f_out:
                json.dump(tx, f_out)

        # Verify batch files exist
        assert os.path.exists(os.path.join(batch_dir, "0.json"))
        assert os.path.exists(os.path.join(batch_dir, "1.json"))
        assert os.path.exists(os.path.join(batch_dir, "2.json"))

        # 1. Step to next item in batch (simulate next_index = 1)
        next_path = os.path.join(batch_dir, "1.json")
        with open(next_path, "r", encoding="utf-8") as f_in:
            next_tx = json.load(f_in)
        assert next_tx["store_name"] == "OMV Gas"
        assert next_tx["amount"] == 45.00

        # 2. Simulate 'create_all': loop through remaining items
        created_items = []
        for rem_idx in range(1, len(raw_txs)):
            rem_path = os.path.join(batch_dir, f"{rem_idx}.json")
            with open(rem_path, "r", encoding="utf-8") as f_r:
                item_data = json.load(f_r)
                created_items.append(item_data["store_name"])

        assert created_items == ["OMV Gas", "Pizzeria"]

    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)

    print("Batch lifecycle simulation passed!")


if __name__ == "__main__":
    test_models_and_validators()
    test_parse_multi_receipt_json()
    test_build_extraction_prompt_multi()
    test_extract_receipt_data_from_text()
    test_batch_lifecycle_simulation()
    print("\nAll multi-transaction text tests passed successfully!")

