import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock dependencies if not installed in host python
for mod in ["app.config", "fastapi", "google", "google.genai", "httpx"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from app.csv_processor import (
    clean_store_and_description,
    detect_card_and_payment_method,
    detect_delimiter,
    guess_category,
    parse_csv_amount,
    parse_csv_date_and_time,
    parse_csv_transactions,
)


def test_parse_csv_amount():
    print("Testing parse_csv_amount...")
    assert parse_csv_amount("-45,90") == 45.90
    assert parse_csv_amount("45,90-") == 45.90
    assert parse_csv_amount("-45.90") == 45.90
    assert parse_csv_amount("(45.90)") == 45.90
    assert parse_csv_amount("1.234,56") == 1234.56
    assert parse_csv_amount("1,234.56") == 1234.56
    assert parse_csv_amount("€ -12,50") == 12.50
    assert parse_csv_amount("15,00 EUR") == 15.00
    assert parse_csv_amount("0.00") is None or parse_csv_amount("0.00") == 0.0
    assert parse_csv_amount("") is None
    print("parse_csv_amount tests passed!")


def test_parse_csv_date_and_time():
    print("Testing parse_csv_date_and_time...")
    d, t = parse_csv_date_and_time("28.10.2026")
    assert d == "2026-10-28"
    assert t == ""

    d, t = parse_csv_date_and_time("28.10.2026 14:35:00")
    assert d == "2026-10-28"
    assert t == "14:35"

    d, t = parse_csv_date_and_time("2026-10-28T18:22:10")
    assert d == "2026-10-28"
    assert t == "18:22"

    d, t = parse_csv_date_and_time("28.10.2026", "14h35")
    assert d == "2026-10-28"
    assert t == "14:35"

    print("parse_csv_date_and_time tests passed!")


def test_detect_delimiter():
    print("Testing detect_delimiter...")
    csv_semi = "Datum;Partnername;Betrag\n28.10.2026;Billa;-12,50\n"
    assert detect_delimiter(csv_semi) == ";"

    csv_comma = "Date,Store,Amount\n2026-10-28,Billa,12.50\n"
    assert detect_delimiter(csv_comma) == ","

    csv_tab = "Date\tStore\tAmount\n2026-10-28\tBilla\t12.50\n"
    assert detect_delimiter(csv_tab) == "\t"
    print("detect_delimiter tests passed!")


def test_detect_card_and_payment_method():
    print("Testing detect_card_and_payment_method...")
    card, method = detect_card_and_payment_method("Kartenzahlung 28.10. BILLA DANKT ...1234")
    assert card == "1234"
    assert method == "card"

    card, method = detect_card_and_payment_method("Kartennr. ****5678")
    assert card == "5678"
    assert method == "card"

    card, method = detect_card_and_payment_method("Barzahlung SPAR")
    assert card is None
    assert method == "cash"
    print("detect_card_and_payment_method tests passed!")


def test_easybank_csv_parsing():
    print("Testing Easybank / BAWAG Austrian CSV parsing...")
    easybank_sample = """Kontoauszug: AT1234567890
Inhaber: Max Mustermann
Zeitraum: 01.10.2026 - 31.10.2026

Buchungsdatum;Valutadatum;Partnername;Buchungstext;Betrag;Währung
28.10.2026;28.10.2026;BILLA DANKT 1234;Kartenzahlung 28.10. 14:35 BILLA DANKT ...1234;-32,45;EUR
27.10.2026;27.10.2026;SPAR DANKT 5678;Kartenzahlung 27.10. 11:20 SPAR DANKT ...5678;-18,90;EUR
26.10.2026;26.10.2026;OEBB TICKETAUTOMAT;Kartenzahlung 26.10. 08:15 OEBB;-9,50;EUR
"""
    asset_accounts = ["Easybank Giro", "BAWAG Credit", "Cash wallet"]
    categories = ["Groceries", "Dining", "Transportation", "Shopping", "Other"]
    budgets = ["Monthly", "Weekly", "Other"]

    mock_settings = MagicMock()
    mock_settings.account_card_mappings = "1234: Easybank Giro, 5678: BAWAG Credit, cash: Cash wallet"

    with patch("app.account_matching.get_settings", return_value=mock_settings):
        txs = parse_csv_transactions(
            easybank_sample,
            default_account="Easybank Giro",
            asset_accounts=asset_accounts,
            categories=categories,
            budgets=budgets,
        )

        assert len(txs) == 3

        # Transaction 1: Billa
        assert txs[0]["date"] == "2026-10-28"
        assert txs[0]["time"] == "14:35"
        assert txs[0]["amount"] == 32.45
        assert "BILLA" in txs[0]["store_name"]
        assert txs[0]["category"] == "Groceries"
        assert txs[0]["source_account"] == "Easybank Giro"
        assert txs[0]["card_last_four"] == "1234"

        # Transaction 2: Spar
        assert txs[1]["date"] == "2026-10-27"
        assert txs[1]["time"] == "11:20"
        assert txs[1]["amount"] == 18.90
        assert "SPAR" in txs[1]["store_name"]
        assert txs[1]["category"] == "Groceries"
        assert txs[1]["source_account"] == "BAWAG Credit"
        assert txs[1]["card_last_four"] == "5678"

        # Transaction 3: OEBB
        assert txs[2]["date"] == "2026-10-26"
        assert txs[2]["time"] == "08:15"
        assert txs[2]["amount"] == 9.50
        assert "OEBB" in txs[2]["store_name"]
        assert txs[2]["category"] == "Transportation"

    print("Easybank / BAWAG Austrian CSV parsing passed!")


def test_standard_english_csv():
    print("Testing standard English CSV...")
    csv_data = """Date,Store,Amount,Category,Description
2026-10-25,Target,42.50,Shopping,Household items
2026-10-26,Trader Joe's,28.75,Groceries,Snacks and produce
2026-10-27,Shell,35.00,Transportation,Gasoline
"""
    asset_accounts = ["Checking", "Credit Card", "Cash wallet"]
    categories = ["Groceries", "Dining", "Transportation", "Shopping", "Other"]

    txs = parse_csv_transactions(
        csv_data,
        default_account="Checking",
        asset_accounts=asset_accounts,
        categories=categories,
    )

    assert len(txs) == 3
    assert txs[0]["date"] == "2026-10-25"
    assert txs[0]["store_name"] == "Target"
    assert txs[0]["amount"] == 42.50
    assert txs[0]["category"] == "Shopping"

    assert txs[1]["date"] == "2026-10-26"
    assert txs[1]["store_name"] == "Trader Joe's"
    assert txs[1]["amount"] == 28.75
    assert txs[1]["category"] == "Groceries"

    assert txs[2]["date"] == "2026-10-27"
    assert txs[2]["store_name"] == "Shell"
    assert txs[2]["amount"] == 35.00
    assert txs[2]["category"] == "Transportation"

    print("Standard English CSV tests passed!")


def test_latin1_and_german_formatting():
    print("Testing Latin-1 encoded CSV with German formatting and trailing minuses...")
    latin1_text = """Buchungstag;Wertstellung;Zahlungsempfänger;Verwendungszweck;Umsatz
28.10.2026;28.10.2026;REWE Markt GmbH;Kartenzahlung Bäckerei;25,50-
27.10.2026;27.10.2026;Müller Großmarkt;Einkauf Büromaterial;1.250,80-
"""
    raw_bytes = latin1_text.encode("latin-1")
    txs = parse_csv_transactions(
        raw_bytes,
        default_account="Checking",
        asset_accounts=["Checking"],
        categories=["Groceries", "Shopping", "Dining", "Other"],
    )

    assert len(txs) == 2
    assert txs[0]["date"] == "2026-10-28"
    assert "REWE" in txs[0]["store_name"]
    assert txs[0]["amount"] == 25.50
    assert txs[0]["category"] in ["Groceries", "Dining"]

    assert txs[1]["date"] == "2026-10-27"
    assert "Müller" in txs[1]["store_name"]
    assert txs[1]["amount"] == 1250.80
    assert txs[1]["category"] in ["Shopping", "Other"]
    print("Latin-1 encoded CSV tests passed!")


def test_debit_credit_columns():
    print("Testing CSV with separate Debit and Credit columns...")
    csv_dc = """Date,Payee,Description,Debit,Credit
2026-10-28,Whole Foods,Groceries,64.20,
2026-10-27,Cafe Central,Coffee and cake,12.50,
"""
    txs = parse_csv_transactions(
        csv_dc,
        default_account="Checking",
        asset_accounts=["Checking"],
        categories=["Groceries", "Dining", "Other"],
    )
    assert len(txs) == 2
    assert txs[0]["amount"] == 64.20
    assert txs[0]["store_name"] == "Whole Foods"
    assert txs[1]["amount"] == 12.50
    assert txs[1]["store_name"] == "Cafe Central"
    print("Debit/Credit column tests passed!")


if __name__ == "__main__":
    test_parse_csv_amount()
    test_parse_csv_date_and_time()
    test_detect_delimiter()
    test_detect_card_and_payment_method()
    test_easybank_csv_parsing()
    test_standard_english_csv()
    test_latin1_and_german_formatting()
    test_debit_credit_columns()
    print("\nAll CSV processor unit tests passed successfully!")
