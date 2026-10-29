import asyncio
import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock dependencies if not installed in host python
for mod in ["app.config", "fastapi", "google", "google.genai", "httpx"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from app.account_matching import match_account_for_receipt
from app.models import PaymentSplit, ReceiptModel


def test_payment_split_model():
    print("Testing PaymentSplit model...")
    # Amount with currency symbol string or float
    split1 = PaymentSplit(payment_method="cash", amount="€ 20,50")
    assert split1.amount == 20.50
    assert split1.payment_method == "cash"
    assert split1.card_last_four is None

    split2 = PaymentSplit(payment_method="card", amount=35.0, card_last_four="1234")
    assert split2.amount == 35.0
    assert split2.card_last_four == "1234"

    # ReceiptModel with splits
    receipt = ReceiptModel(
        date="2026-10-28",
        amount=55.50,
        store_name="Billa Plus",
        description="Groceries",
        category="Groceries",
        budget="Monthly",
        is_split_payment=True,
        splits=[split1, split2],
    )
    assert receipt.is_split_payment is True
    assert len(receipt.splits) == 2
    assert receipt.splits[0].amount == 20.50
    assert receipt.splits[1].amount == 35.0
    print("PaymentSplit and ReceiptModel tests passed!")


def test_enrich_extracted_data_accounts():
    print("Testing enrich_extracted_data_accounts logic...")
    from app.account_matching import enrich_extracted_data_accounts

    asset_accounts = ["Cash wallet", "Easybank Giro", "BAWAG Credit"]

    mock_settings = MagicMock()
    mock_settings.account_card_mappings = "1234: Easybank Giro, 5678: BAWAG Credit, cash: Cash wallet"

    with patch("app.account_matching.get_settings", return_value=mock_settings):
        extracted = {
            "date": "2026-10-28",
            "amount": 55.0,
            "store_name": "Spar",
            "description": "Lunch and supplies",
            "category": "Groceries",
            "budget": "Monthly",
            "is_split_payment": True,
            "payment_method": "split",
            "splits": [
                {"payment_method": "cash", "amount": 20.0, "card_last_four": None},
                {"payment_method": "card", "amount": 35.0, "card_last_four": "5678"},
            ],
        }

        enriched = enrich_extracted_data_accounts(extracted, asset_accounts)
        assert len(enriched["splits"]) == 2
        # First split (cash) should match "Cash wallet"
        assert enriched["splits"][0]["account"] == "Cash wallet"
        assert enriched["splits"][0]["amount"] == 20.0
        # Second split (card *5678) should match "BAWAG Credit"
        assert enriched["splits"][1]["account"] == "BAWAG Credit"
        assert enriched["splits"][1]["amount"] == 35.0
    print("enrich_extracted_data_accounts tests passed!")


def test_create_transaction_from_data_split():
    print("Testing create_transaction_from_data with split payments...")
    from app.receipt_processing import create_transaction_from_data

    created_txs = []
    attached_journals = []

    def mock_create_firefly_transaction(receipt, source_account, tags=None):
        tx_id = f"tx_{len(created_txs) + 1}"
        journal_id = 100 + len(created_txs)
        created_txs.append(
            {
                "id": tx_id,
                "journal_id": journal_id,
                "amount": receipt.amount,
                "source_account": source_account,
                "tags": tags,
                "description": receipt.description,
            }
        )
        return {
            "data": {
                "id": tx_id,
                "attributes": {
                    "transactions": [{"transaction_journal_id": journal_id}]
                },
            }
        }

    def mock_attach_image(journal_id, image_bytes, filename):
        attached_journals.append((journal_id, filename))
        return True

    with patch(
        "app.receipt_processing.create_firefly_transaction",
        side_effect=mock_create_firefly_transaction,
    ), patch(
        "app.receipt_processing.attach_image_to_transaction",
        side_effect=mock_attach_image,
    ):
        receipt_data = {
            "date": "2026-10-28",
            "amount": 55.0,
            "store_name": "Interspar",
            "description": "Weekly Groceries",
            "category": "Groceries",
            "budget": "Monthly",
            "splits": [
                {"account": "Cash wallet", "amount": 20.0},
                {"account": "Easybank Giro", "amount": 35.0},
            ],
        }

        fake_image = b"test-fake-receipt-image"
        result = asyncio.run(
            create_transaction_from_data(
                receipt_data,
                source_account="Cash wallet",
                image_bytes=fake_image,
                filename="receipt_Interspar_2026-10-28.jpg",
            )
        )

        assert "Split payment created" in result
        assert "Cash wallet (20.00)" in result
        assert "Easybank Giro (35.00)" in result

        # Verify 2 transactions were created in Firefly III
        assert len(created_txs) == 2
        # Check split 1
        assert created_txs[0]["source_account"] == "Cash wallet"
        assert created_txs[0]["amount"] == 20.0
        assert "Split 1/2" in created_txs[0]["description"]
        assert "split-payment" in created_txs[0]["tags"]
        assert "automated" in created_txs[0]["tags"]

        # Check split 2
        assert created_txs[1]["source_account"] == "Easybank Giro"
        assert created_txs[1]["amount"] == 35.0
        assert "Split 2/2" in created_txs[1]["description"]
        assert "split-payment" in created_txs[1]["tags"]
        assert "automated" in created_txs[1]["tags"]

        # Verify image was attached to both journals
        assert len(attached_journals) == 2
        assert attached_journals[0][0] == 100
        assert attached_journals[1][0] == 101

    print("create_transaction_from_data split tests passed!")


def test_split_payload_parsing():
    print("Testing split_payload JSON handling...")
    raw_payload = json.dumps(
        [
            {"account": "Cash wallet", "amount": 15.50},
            {"account": "Easybank Giro", "amount": 40.00},
            {"account": "Invalid zero amount", "amount": 0.0},
        ]
    )

    parsed = json.loads(raw_payload)
    valid_splits = []
    for sp in parsed:
        if isinstance(sp, dict):
            amt = float(sp.get("amount", 0.0))
            acc = (sp.get("account") or "").strip()
            if amt > 0 and acc:
                valid_splits.append({"account": acc, "amount": amt})

    assert len(valid_splits) == 2
    assert valid_splits[0]["account"] == "Cash wallet"
    assert valid_splits[0]["amount"] == 15.50
    assert valid_splits[1]["account"] == "Easybank Giro"
    assert valid_splits[1]["amount"] == 40.00
    print("split_payload JSON parsing tests passed!")


if __name__ == "__main__":
    test_payment_split_model()
    test_enrich_extracted_data_accounts()
    test_create_transaction_from_data_split()
    test_split_payload_parsing()
    print("\nAll split payment unit tests passed successfully!")
