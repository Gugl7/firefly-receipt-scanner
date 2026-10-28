import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock dependencies if not installed in host python
for mod in ["app.config", "fastapi", "google", "google.genai", "httpx"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from app.account_matching import (
    clean_card_digits,
    match_account_for_receipt,
    parse_account_card_mappings,
)
from app.models import ReceiptModel


def test_clean_card_digits():
    print("Testing clean_card_digits...")
    assert clean_card_digits("1234") == "1234"
    assert clean_card_digits("**** 1234") == "1234"
    assert clean_card_digits("************5678") == "5678"
    assert clean_card_digits("...9876") == "9876"
    assert clean_card_digits("PAN: 1234567890123456") == "3456"
    assert clean_card_digits("12") is None  # Too short
    assert clean_card_digits("cash") is None
    assert clean_card_digits(None) is None
    assert clean_card_digits("") is None
    print("clean_card_digits tests passed!")


def test_parse_account_card_mappings():
    print("Testing parse_account_card_mappings...")
    # Comma-separated string
    s1 = "1234: Easybank Giro, 5678: BAWAG Credit, cash: Cash wallet"
    res1 = parse_account_card_mappings(s1)
    assert res1["1234"] == "Easybank Giro"
    assert res1["5678"] == "BAWAG Credit"
    assert res1["cash"] == "Cash wallet"

    # JSON string
    s2 = '{"1234": "Easybank Giro", "5678": "BAWAG Credit", "cash": "Cash wallet"}'
    res2 = parse_account_card_mappings(s2)
    assert res2["1234"] == "Easybank Giro"
    assert res2["cash"] == "Cash wallet"

    # Multiple cards for the same account:
    # A. Multiple separate entries
    s3 = "1111: Easybank Giro, 2222: Easybank Giro, 3333: BAWAG Credit"
    res3 = parse_account_card_mappings(s3)
    assert res3["1111"] == "Easybank Giro"
    assert res3["2222"] == "Easybank Giro"
    assert res3["3333"] == "BAWAG Credit"

    # B. Slash / pipe separated cards
    s4 = "1111/2222: Easybank Giro, 3333|4444: BAWAG Credit"
    res4 = parse_account_card_mappings(s4)
    assert res4["1111"] == "Easybank Giro"
    assert res4["2222"] == "Easybank Giro"
    assert res4["3333"] == "BAWAG Credit"
    assert res4["4444"] == "BAWAG Credit"

    # C. JSON with account as key and list of cards as value
    s5 = '{"Easybank Giro": ["1111", "2222"], "BAWAG Credit": ["3333"]}'
    res5 = parse_account_card_mappings(s5)
    assert res5["1111"] == "Easybank Giro"
    assert res5["2222"] == "Easybank Giro"
    assert res5["3333"] == "BAWAG Credit"

    # D. JSON with multiple cards in key
    s6 = '{"1111, 2222": "Easybank Giro"}'
    res6 = parse_account_card_mappings(s6)
    assert res6["1111"] == "Easybank Giro"
    assert res6["2222"] == "Easybank Giro"

    # Empty
    assert parse_account_card_mappings("") == {}
    assert parse_account_card_mappings(None) == {}
    print("parse_account_card_mappings tests passed!")


def test_match_account_for_receipt():
    print("Testing match_account_for_receipt...")
    asset_accounts = ["Cash wallet", "Easybank Giro", "BAWAG Mastercard", "Savings Account"]
    mock_settings = MagicMock()
    mock_settings.account_card_mappings = "1234: Easybank Giro, 5678: BAWAG Mastercard, cash: Cash wallet"

    with patch("app.account_matching.get_settings", return_value=mock_settings):
        # 1. Card ending in 1234 -> Easybank Giro
        acc, reason = match_account_for_receipt("card", "1234", asset_accounts)
        assert acc == "Easybank Giro"
        assert "1234" in reason

        # 2. Card ending in **** 5678 -> BAWAG Mastercard
        acc, reason = match_account_for_receipt("card", "**** 5678", asset_accounts)
        assert acc == "BAWAG Mastercard"
        assert "5678" in reason

        # 3. Cash payment -> Cash wallet
        acc, reason = match_account_for_receipt("cash", None, asset_accounts)
        assert acc == "Cash wallet"
        assert "Cash" in reason

        # 4. Unknown card -> None
        acc, reason = match_account_for_receipt("card", "9999", asset_accounts)
        assert acc is None
        assert reason is None

    # Fallback to account containing digits if not explicitly mapped
    mock_settings_empty = MagicMock()
    mock_settings_empty.account_card_mappings = ""
    accounts_with_card = ["Easybank (card 4321)", "Cash wallet"]
    with patch("app.account_matching.get_settings", return_value=mock_settings_empty):
        acc, reason = match_account_for_receipt("card", "4321", accounts_with_card)
        assert acc == "Easybank (card 4321)"
        assert "4321" in reason

    # Fallback for cash to account with 'bargeld' if not explicitly mapped
    accounts_german = ["Bargeld", "Girokonto"]
    with patch("app.account_matching.get_settings", return_value=mock_settings_empty):
        acc, reason = match_account_for_receipt("cash", None, accounts_german)
        assert acc == "Bargeld"
        assert "Cash" in reason

    print("match_account_for_receipt tests passed!")


def test_receipt_model_with_payment_fields():
    print("Testing ReceiptModel with payment fields...")
    r = ReceiptModel(
        date="2026-10-29",
        amount=35.0,
        store_name="Billa",
        description="Groceries",
        category="Groceries",
        budget="Monthly",
        payment_method="card",
        card_last_four="1234",
    )
    assert r.payment_method == "card"
    assert r.card_last_four == "1234"
    print("ReceiptModel payment fields test passed!")


if __name__ == "__main__":
    test_clean_card_digits()
    test_parse_account_card_mappings()
    test_match_account_for_receipt()
    test_receipt_model_with_payment_fields()
    print("\nAll account matching unit tests passed successfully!")

