import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock app.config if pydantic is not compatible with host python version
if "app.config" not in sys.modules:
    mock_config = MagicMock()
    sys.modules["app.config"] = mock_config

from app.firefly import check_duplicate_transactions


def test_duplicate_exact_match():
    print("Testing exact match...")
    mock_settings = MagicMock()
    mock_settings.firefly_iii_url = "http://firefly.test"
    mock_settings.firefly_iii_token = "secret"
    mock_settings.firefly_api_url = "http://firefly.test/api/v1/"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {
                "id": "101",
                "attributes": {
                    "transactions": [
                        {
                            "transaction_journal_id": "201",
                            "type": "withdrawal",
                            "date": "2026-10-27T12:00:00+00:00",
                            "amount": "42.50",
                            "currency_symbol": "$",
                            "description": "Target store shopping",
                            "destination_name": "Target",
                            "source_name": "Checking Account",
                        }
                    ]
                },
            }
        ]
    }

    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.get", return_value=mock_resp
    ) as mock_get:
        dups = check_duplicate_transactions("2026-10-27", 42.50, "Target")
        assert len(dups) == 1, f"Expected 1 duplicate, got {len(dups)}"
        assert dups[0]["confidence"] == "high"
        assert dups[0]["destination_name"] == "Target"
        assert dups[0]["amount"] == 42.50

        # Check query params had 3-day window [2026-10-26, 2026-10-28]
        args, kwargs = mock_get.call_args
        assert kwargs["params"]["start"] == "2026-10-26"
        assert kwargs["params"]["end"] == "2026-10-28"

    print("Exact match test passed!")


def test_duplicate_different_amount():
    print("Testing different amount (no match)...")
    mock_settings = MagicMock()
    mock_settings.firefly_iii_url = "http://firefly.test"
    mock_settings.firefly_iii_token = "secret"
    mock_settings.firefly_api_url = "http://firefly.test/api/v1/"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {
                "id": "102",
                "attributes": {
                    "transactions": [
                        {
                            "transaction_journal_id": "202",
                            "type": "withdrawal",
                            "date": "2026-10-27T12:00:00+00:00",
                            "amount": "15.00",
                            "destination_name": "Target",
                        }
                    ]
                },
            }
        ]
    }

    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.get", return_value=mock_resp
    ):
        dups = check_duplicate_transactions("2026-10-27", 42.50, "Target")
        assert len(dups) == 0, f"Expected 0 duplicates, got {len(dups)}"

    print("Different amount test passed!")


def test_duplicate_yesterday_same_store():
    print("Testing yesterday match with same store...")
    mock_settings = MagicMock()
    mock_settings.firefly_iii_url = "http://firefly.test"
    mock_settings.firefly_iii_token = "secret"
    mock_settings.firefly_api_url = "http://firefly.test/api/v1/"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {
                "id": "103",
                "attributes": {
                    "transactions": [
                        {
                            "transaction_journal_id": "203",
                            "type": "withdrawal",
                            "date": "2026-10-26T15:30:00+00:00",
                            "amount": "42.50",
                            "destination_name": "Target",
                            "source_name": "Cash wallet",
                        }
                    ]
                },
            }
        ]
    }

    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.get", return_value=mock_resp
    ):
        dups = check_duplicate_transactions("2026-10-27", 42.50, "Target")
        assert len(dups) == 1
        assert dups[0]["confidence"] == "medium"
        assert "yesterday" in dups[0]["reason"]

    print("Yesterday match test passed!")


def test_duplicate_skip_deposits():
    print("Testing skip deposits...")
    mock_settings = MagicMock()
    mock_settings.firefly_iii_url = "http://firefly.test"
    mock_settings.firefly_iii_token = "secret"
    mock_settings.firefly_api_url = "http://firefly.test/api/v1/"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {
                "id": "104",
                "attributes": {
                    "transactions": [
                        {
                            "transaction_journal_id": "204",
                            "type": "deposit",
                            "date": "2026-10-27T12:00:00+00:00",
                            "amount": "42.50",
                            "destination_name": "Target",
                        }
                    ]
                },
            }
        ]
    }

    with patch("app.firefly.get_settings", return_value=mock_settings), patch(
        "app.firefly.requests.get", return_value=mock_resp
    ):
        dups = check_duplicate_transactions("2026-10-27", 42.50, "Target")
        assert len(dups) == 0

    print("Skip deposits test passed!")


if __name__ == "__main__":
    test_duplicate_exact_match()
    test_duplicate_different_amount()
    test_duplicate_yesterday_same_store()
    test_duplicate_skip_deposits()
    print("\nAll duplicate detection unit tests passed successfully!")

