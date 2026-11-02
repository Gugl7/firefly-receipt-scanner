import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
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
from app.models import MultiReceiptModel, ReceiptModel
from app.receipt_processing import (
    build_extraction_prompt,
    extract_receipt_data,
    extract_receipt_data_from_text,
    refine_csv_transactions_with_llm,
)


def test_build_extraction_prompt_system_prompt():
    print("Testing build_extraction_prompt with and without system_prompt...")
    categories = ["Groceries", "Dining", "Shopping"]
    budgets = ["Monthly", "Weekly"]

    # 1. Image mode without system_prompt
    p_img_no_sp = build_extraction_prompt(categories, budgets, receipt_text=None, system_prompt=None)
    assert "--- USER CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---" not in p_img_no_sp
    assert "Please analyze the attached receipt image" in p_img_no_sp

    # 2. Image mode with system_prompt
    sp_text = "Pay close attention to handwritten tips; total is at the bottom right"
    p_img_sp = build_extraction_prompt(categories, budgets, receipt_text=None, system_prompt=sp_text)
    assert "--- USER CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---" in p_img_sp
    assert sp_text in p_img_sp
    assert "Please analyze the attached receipt image" in p_img_sp

    # 3. Text mode without system_prompt
    p_txt_no_sp = build_extraction_prompt(categories, budgets, receipt_text="Coffee 4.50", system_prompt="")
    assert "--- USER CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---" not in p_txt_no_sp
    assert "Coffee 4.50" in p_txt_no_sp

    # 4. Text mode with system_prompt
    p_txt_sp = build_extraction_prompt(
        categories, budgets, receipt_text="Coffee 4.50", system_prompt="Set budget to Daily Treats"
    )
    assert "--- USER CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---" in p_txt_sp
    assert "Set budget to Daily Treats" in p_txt_sp
    assert "Coffee 4.50" in p_txt_sp

    print("build_extraction_prompt system prompt tests passed!")


def test_extract_receipt_data_forwards_system_prompt():
    print("Testing extract_receipt_data forwards system_prompt to LLM...")

    fake_receipt = ReceiptModel(
        date="2026-10-28",
        time="14:35",
        amount=55.00,
        store_name="Handwritten Store",
        description="Dinner",
        category="Dining",
        budget="Monthly",
    )

    captured_prompt = None

    async def mock_run_llm(prompt, image=None, image_base64=None, settings=None, response_model=ReceiptModel):
        nonlocal captured_prompt
        captured_prompt = prompt
        return fake_receipt

    async def _run():
        nonlocal captured_prompt
        with patch("app.receipt_processing.process_image", return_value=(MagicMock(), b"fake_bytes")), \
             patch("app.receipt_processing.get_firefly_categories", return_value=["Dining", "Other"]), \
             patch("app.receipt_processing.get_firefly_budgets", return_value=["Monthly"]), \
             patch("app.receipt_processing._run_llm_extraction", side_effect=mock_run_llm):

            # Without system prompt
            await extract_receipt_data(b"image_content", filename="test.jpg", system_prompt=None)
            assert "--- USER CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---" not in captured_prompt

            # With system prompt
            custom_sp = "Check handwriting for waiter tip"
            res = await extract_receipt_data(b"image_content", filename="test.jpg", system_prompt=custom_sp)
            assert "--- USER CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---" in captured_prompt
            assert custom_sp in captured_prompt
            assert res["amount"] == 55.00
            assert res["store_name"] == "Handwritten Store"

    asyncio.run(_run())
    print("extract_receipt_data system prompt tests passed!")


def test_extract_receipt_data_from_text_forwards_system_prompt():
    print("Testing extract_receipt_data_from_text forwards system_prompt to LLM...")

    fake_multi = MultiReceiptModel(
        transactions=[
            ReceiptModel(
                date="2026-10-28",
                time=None,
                amount=12.00,
                store_name="Bakery",
                description="Pastries",
                category="Dining",
                budget="Weekly",
            )
        ]
    )

    captured_prompt = None

    async def mock_run_llm(prompt, image=None, image_base64=None, settings=None, response_model=MultiReceiptModel):
        nonlocal captured_prompt
        captured_prompt = prompt
        return fake_multi

    async def _run():
        nonlocal captured_prompt
        with patch("app.receipt_processing.get_firefly_categories", return_value=["Dining", "Other"]), \
             patch("app.receipt_processing.get_firefly_budgets", return_value=["Weekly"]), \
             patch("app.receipt_processing._run_llm_extraction", side_effect=mock_run_llm):

            custom_sp = "All pastries belong to Daily Snacks budget"
            txs = await extract_receipt_data_from_text("Bakery EUR 12.00", system_prompt=custom_sp)
            assert "--- USER CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---" in captured_prompt
            assert custom_sp in captured_prompt
            assert len(txs) == 1
            assert txs[0]["store_name"] == "Bakery"

    asyncio.run(_run())
    print("extract_receipt_data_from_text system prompt tests passed!")


def test_refine_csv_transactions_with_llm():
    print("Testing refine_csv_transactions_with_llm...")

    sample_txs = [
        {
            "date": "2026-10-28",
            "time": "12:00",
            "amount": 45.00,
            "store_name": "PAYPAL *LIDL",
            "description": "PAYPAL *LIDL 1234",
            "category": "Other",
            "budget": "Other",
            "payment_method": "card",
            "card_last_four": "",
            "source_account": "Checking",
            "transaction_type": "withdrawal",
            "destination_account": "",
        },
        {
            "date": "2026-10-27",
            "time": "09:30",
            "amount": 100.00,
            "store_name": "ATM Cash",
            "description": "ATM Cash Withdrawal",
            "category": "Other",
            "budget": "Other",
            "payment_method": "cash",
            "card_last_four": "9999",
            "source_account": "Checking",
            "transaction_type": "withdrawal",
            "destination_account": "",
        },
    ]

    # 1. Empty system prompt returns original transactions immediately without LLM
    async def _run_empty():
        res = await refine_csv_transactions_with_llm(sample_txs, system_prompt="")
        assert res == sample_txs
        res_none = await refine_csv_transactions_with_llm(sample_txs, system_prompt=None)
        assert res_none == sample_txs

    asyncio.run(_run_empty())

    # 2. System prompt refines categories, store names, and transfer fields
    refined_models = MultiReceiptModel(
        transactions=[
            ReceiptModel(
                date="2026-10-28",
                time="12:00",
                amount=45.00,
                store_name="Lidl",
                description="Groceries at Lidl",
                category="Groceries",
                budget="Weekly Groceries",
                payment_method="card",
                card_last_four="1234",
                transaction_type="withdrawal",
                destination_account=None,
            ),
            ReceiptModel(
                date="2026-10-27",
                time="09:30",
                amount=100.00,
                store_name="Bankomat",
                description="ATM Cash Withdrawal",
                category="General",
                budget=None,
                payment_method="cash",
                card_last_four="9999",
                transaction_type="transfer",
                destination_account="Cash wallet",
            ),
        ]
    )

    captured_prompt = None

    async def mock_run_llm(prompt, image=None, image_base64=None, settings=None, response_model=MultiReceiptModel):
        nonlocal captured_prompt
        captured_prompt = prompt
        return refined_models

    async def _run_refinement():
        nonlocal captured_prompt
        with patch("app.receipt_processing._run_llm_extraction", side_effect=mock_run_llm):
            sp = "Lidl is Groceries with budget Weekly Groceries. ATM Cash is a transfer to Cash wallet with no budget."
            refined = await refine_csv_transactions_with_llm(
                sample_txs,
                system_prompt=sp,
                categories=["Groceries", "General", "Other"],
                budgets=["Weekly Groceries", "Other"],
                asset_accounts=["Checking", "Cash wallet"],
            )

            assert "--- USER CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---" in captured_prompt
            assert sp in captured_prompt
            assert len(refined) == 2

            # Lidl item refined
            assert refined[0]["store_name"] == "Lidl"
            assert refined[0]["category"] == "Groceries"
            assert refined[0]["budget"] == "Weekly Groceries"
            assert refined[0]["card_last_four"] == "1234"

            # ATM Cash item refined to transfer
            assert refined[1]["store_name"] == "Bankomat"
            assert refined[1]["transaction_type"] == "transfer"
            assert refined[1]["destination_account"] == "Cash wallet"
            assert refined[1]["budget"] == ""  # None budget becomes empty string

    asyncio.run(_run_refinement())

    # 3. Graceful fallback on LLM exception
    async def mock_failing_llm(*args, **kwargs):
        raise RuntimeError("LLM network timeout")

    async def _run_fallback():
        with patch("app.receipt_processing._run_llm_extraction", side_effect=mock_failing_llm):
            fallback_res = await refine_csv_transactions_with_llm(
                sample_txs,
                system_prompt="Some prompt",
                categories=["Groceries"],
            )
            # Should not raise, falls back to original items
            assert len(fallback_res) == 2
            assert fallback_res[0]["store_name"] == "PAYPAL *LIDL"

    asyncio.run(_run_fallback())
    print("refine_csv_transactions_with_llm tests passed!")


def test_batch_system_prompt_persistence():
    print("Testing batch system_prompt.txt persistence across sequential items...")

    batch_id = str(uuid.uuid4())
    batch_dir = os.path.join(tempfile.gettempdir(), "receipt_scanner", batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    try:
        sp_content = "Focus on invoice number in top left"
        with open(os.path.join(batch_dir, "system_prompt.txt"), "w", encoding="utf-8") as f:
            f.write(sp_content)

        # Read back during next item processing
        sp_read = ""
        sp_path = os.path.join(batch_dir, "system_prompt.txt")
        if os.path.exists(sp_path):
            with open(sp_path, "r", encoding="utf-8") as f:
                sp_read = f.read().strip()

        assert sp_read == sp_content
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)

    print("Batch system prompt persistence tests passed!")


def test_csv_app_import_and_refinement():
    print("Testing get_firefly_budgets availability in app/app.py...")
    import ast
    with open("app/app.py", "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("firefly", ".firefly"):
            for n in node.names:
                imported_names.add(n.name)
    assert "get_firefly_budgets" in imported_names
    print("get_firefly_budgets is properly imported in app/app.py!")


if __name__ == "__main__":
    test_build_extraction_prompt_system_prompt()
    test_extract_receipt_data_forwards_system_prompt()
    test_extract_receipt_data_from_text_forwards_system_prompt()
    test_refine_csv_transactions_with_llm()
    test_batch_system_prompt_persistence()
    test_csv_app_import_and_refinement()
    print("\nAll system prompt tests passed successfully!")

