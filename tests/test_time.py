import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock dependencies if not installed in host python
for mod in ["app.config", "fastapi", "google", "google.genai", "httpx"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from app.firefly import create_firefly_transaction
from app.models import ReceiptModel
from app.receipt_processing import (
    extract_time_from_date_string,
    normalize_date,
    normalize_time,
)


def test_normalize_time():
    print("Testing normalize_time...")
    assert normalize_time("14:35") == "14:35"
    assert normalize_time("14:35:22") == "14:35"
    assert normalize_time("2:35 PM") == "14:35"
    assert normalize_time("02:35 PM") == "14:35"
    assert normalize_time("2:35pm") == "14:35"
    assert normalize_time("9:15 AM") == "09:15"
    assert normalize_time("09:15 AM") == "09:15"
    assert normalize_time("12:00 PM") == "12:00"
    assert normalize_time("12:15 AM") == "00:15"
    assert normalize_time("14h35") == "14:35"
    assert normalize_time("14.35") == "14:35"
    assert normalize_time(None) is None
    assert normalize_time("") is None
    assert normalize_time("null") is None
    assert normalize_time("none") is None
    assert normalize_time("unknown") is None
    print("normalize_time tests passed!")


def test_normalize_date_with_time():
    print("Testing normalize_date with and without time...")
    assert normalize_date("2026-10-28") == "2026-10-28"
    assert normalize_date("28.10.2026") == "2026-10-28"
    assert normalize_date("28/10/2026") == "2026-10-28"
    assert normalize_date("2026-10-28T14:35:00") == "2026-10-28"
    assert normalize_date("2026-10-28 14:35") == "2026-10-28"
    print("normalize_date tests passed!")


def test_extract_time_from_date_string():
    print("Testing extract_time_from_date_string...")
    assert extract_time_from_date_string("2026-10-28T14:35:00") == "14:35"
    assert extract_time_from_date_string("2026-10-28 14:35") == "14:35"
    assert extract_time_from_date_string("2026-10-28 2:35 PM") == "14:35"
    assert extract_time_from_date_string("2026-10-28") is None
    print("extract_time_from_date_string tests passed!")


def test_receipt_model():
    print("Testing ReceiptModel with and without time...")
    # Without time (backward compatibility)
    r1 = ReceiptModel(
        date="2026-10-28",
        amount=19.99,
        store_name="Spar",
        description="Snacks",
        category="Food",
        budget="Groceries",
    )
    assert r1.date == "2026-10-28"
    assert r1.time is None

    # With time
    r2 = ReceiptModel(
        date="2026-10-28",
        time="14:35",
        amount=19.99,
        store_name="Spar",
        description="Snacks",
        category="Food",
        budget="Groceries",
    )
    assert r2.date == "2026-10-28"
    assert r2.time == "14:35"
    print("ReceiptModel tests passed!")


def test_create_firefly_transaction_with_time():
    print("Testing create_firefly_transaction date and time payload formatting...")
    mock_settings = MagicMock()
    mock_settings.firefly_iii_url = "http://firefly.test"
    mock_settings.firefly_iii_token = "secret"
    mock_settings.firefly_api_url = "http://firefly.test/api/v1/"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": {"id": "123"}}

    # Receipt with explicit time
    receipt_with_time = MagicMock()
    receipt_with_time.date = "2026-10-28"
    receipt_with_time.time = "14:35"
    receipt_with_time.amount = 25.50
    receipt_with_time.description = "Lunch"
    receipt_with_time.store_name = "Bistro"
    receipt_with_time.category = "Dining"
    receipt_with_time.budget = "Monthly"

    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.post", return_value=mock_resp
    ) as mock_post:
        create_firefly_transaction(receipt_with_time, "Checking")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        tx = payload["transactions"][0]
        assert tx["date"] == "2026-10-28T14:35:00", f"Expected 2026-10-28T14:35:00, got {tx['date']}"

    # Receipt without time
    receipt_no_time = MagicMock()
    receipt_no_time.date = "2026-10-28"
    receipt_no_time.time = None
    receipt_no_time.amount = 10.00
    receipt_no_time.description = "Item"
    receipt_no_time.store_name = "Shop"
    receipt_no_time.category = "Other"
    receipt_no_time.budget = "Other"

    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.post", return_value=mock_resp
    ) as mock_post:
        create_firefly_transaction(receipt_no_time, "Checking")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        tx = payload["transactions"][0]
        assert tx["date"] == "2026-10-28T00:00:00", f"Expected 2026-10-28T00:00:00, got {tx['date']}"

    print("create_firefly_transaction date and time tests passed!")


if __name__ == "__main__":
    test_normalize_time()
    test_normalize_date_with_time()
    test_extract_time_from_date_string()
    test_receipt_model()
    test_create_firefly_transaction_with_time()
    print("\nAll date and time tests passed successfully!")
