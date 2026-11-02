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


def test_csv_batch_simulation():
    print("Testing CSV batch lifecycle simulation...")

    csv_data = """Date,Store,Amount,Category,Description
2026-10-25,Target,42.50,Shopping,Household items
2026-10-26,Trader Joe's,28.75,Groceries,Snacks and produce
2026-10-27,Shell,35.00,Transportation,Gasoline
"""
    asset_accounts = ["Checking", "Credit Card", "Cash wallet"]
    categories = ["Groceries", "Dining", "Transportation", "Shopping", "Other"]

    # 1. Parse CSV transactions
    parsed_txs = parse_csv_transactions(
        csv_data,
        default_account="Checking",
        asset_accounts=asset_accounts,
        categories=categories,
    )
    assert len(parsed_txs) == 3

    # 2. Simulate saving to batch session directory
    batch_id = str(uuid.uuid4())
    batch_dir = os.path.join(tempfile.gettempdir(), "receipt_scanner", batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    try:
        for idx, tx_data in enumerate(parsed_txs):
            target_json = os.path.join(batch_dir, f"{idx}.json")
            with open(target_json, "w", encoding="utf-8") as f_out:
                json.dump(tx_data, f_out, ensure_ascii=False)

        # 3. Simulate first item review (index 0)
        first_item = parsed_txs[0]
        assert first_item["store_name"] == "Target"
        assert first_item["amount"] == 42.50

        # 4. Simulate advancing to next item (index 1)
        next_index = 1
        next_json_path = os.path.join(batch_dir, f"{next_index}.json")
        assert os.path.exists(next_json_path)

        with open(next_json_path, "r", encoding="utf-8") as f_in:
            next_data = json.load(f_in)

        assert next_data["store_name"] == "Trader Joe's"
        assert next_data["amount"] == 28.75
        assert next_data["category"] == "Groceries"

        # 5. Simulate advancing to last item (index 2)
        next_index = 2
        next_json_path = os.path.join(batch_dir, f"{next_index}.json")
        assert os.path.exists(next_json_path)

        with open(next_json_path, "r", encoding="utf-8") as f_in:
            last_data = json.load(f_in)

        assert last_data["store_name"] == "Shell"
        assert last_data["amount"] == 35.00

    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)

    print("CSV batch lifecycle simulation passed!")


if __name__ == "__main__":
    test_csv_batch_simulation()
    print("\nAll CSV batch tests passed successfully!")

