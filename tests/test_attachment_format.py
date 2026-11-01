import asyncio
import base64
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock dependencies if not installed in host python
for mod in ["app.config", "fastapi", "google", "google.genai", "httpx"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from app.image_utils import (
    build_attachment_filename,
    detect_file_extension,
    sanitize_filename,
)
from app.models import ReceiptModel


def test_detect_file_extension():
    print("Testing detect_file_extension...")
    # 1. PDF magic bytes
    pdf_bytes = b"%PDF-1.5 test pdf content"
    assert detect_file_extension("receipt.jpg", pdf_bytes) == ".pdf"
    assert detect_file_extension(None, pdf_bytes) == ".pdf"

    # 2. PDF filename
    assert detect_file_extension("invoice.pdf", None) == ".pdf"
    assert detect_file_extension("INVOICE.PDF", None) == ".pdf"

    # 3. PNG magic bytes
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    assert detect_file_extension("image", png_bytes) == ".png"
    assert detect_file_extension("test.png", None) == ".png"

    # 4. JPEG magic bytes
    jpg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    assert detect_file_extension("photo", jpg_bytes) == ".jpg"
    assert detect_file_extension("photo.jpeg", None) == ".jpg"
    assert detect_file_extension("photo.jpg", None) == ".jpg"

    # 5. WebP magic bytes
    webp_bytes = b"RIFF\x00\x00\x00\x00WEBPVP8 "
    assert detect_file_extension("test", webp_bytes) == ".webp"
    assert detect_file_extension("test.webp", None) == ".webp"

    # 6. Fallback
    assert detect_file_extension(None, None) == ".jpg"
    print("detect_file_extension tests passed!")


def test_build_attachment_filename():
    print("Testing build_attachment_filename...")
    pdf_bytes = b"%PDF-1.7 sample"
    fname_pdf = build_attachment_filename(
        store_name="Billa Plus",
        date_str="2026-10-28",
        original_filename="rechnung.pdf",
        file_bytes=pdf_bytes,
    )
    assert fname_pdf == "receipt_Billa_Plus_2026-10-28.pdf"
    assert fname_pdf.endswith(".pdf")

    png_bytes = b"\x89PNG\r\n\x1a\n"
    fname_png = build_attachment_filename(
        store_name="Spar Express",
        date_str="2026-10-28",
        original_filename="screenshot.png",
        file_bytes=png_bytes,
    )
    assert fname_png == "receipt_Spar_Express_2026-10-28.png"
    assert fname_png.endswith(".png")

    jpg_bytes = b"\xff\xd8\xff"
    fname_jpg = build_attachment_filename(
        store_name="Hofer KG",
        date_str="2026-10-28",
        original_filename="photo.jpg",
        file_bytes=jpg_bytes,
    )
    assert fname_jpg == "receipt_Hofer_KG_2026-10-28.jpg"
    assert fname_jpg.endswith(".jpg")
    print("build_attachment_filename tests passed!")


def test_create_transaction_from_data_pdf_attachment():
    print("Testing create_transaction_from_data attaches PDF preserving .pdf format...")
    from app.receipt_processing import create_transaction_from_data

    attached_calls = []

    def mock_create_firefly_transaction(receipt, source_account, tags=None):
        return {
            "data": {
                "id": "123",
                "attributes": {
                    "transactions": [{"transaction_journal_id": 456}]
                },
            }
        }

    def mock_attach(journal_id, file_bytes, filename):
        attached_calls.append((journal_id, file_bytes, filename))
        return True

    with patch(
        "app.receipt_processing.create_firefly_transaction",
        side_effect=mock_create_firefly_transaction,
    ), patch(
        "app.receipt_processing.attach_image_to_transaction",
        side_effect=mock_attach,
    ):
        receipt_data = {
            "date": "2026-10-28",
            "time": "14:35",
            "amount": 45.90,
            "store_name": "Billa Plus",
            "description": "Weekly groceries",
            "category": "Groceries",
            "budget": "Monthly",
        }
        pdf_bytes = b"%PDF-1.4 original binary pdf bytes here"
        pdf_filename = "receipt_Billa_Plus_2026-10-28.pdf"

        res = asyncio.run(
            create_transaction_from_data(
                receipt_data,
                source_account="Easybank Giro",
                image_bytes=pdf_bytes,
                filename=pdf_filename,
            )
        )

        assert "123" in res
        assert len(attached_calls) == 1
        journal_id, attached_bytes, attached_name = attached_calls[0]
        assert journal_id == 456
        assert attached_bytes == pdf_bytes  # Exact PDF bytes, NOT converted
        assert attached_name == "receipt_Billa_Plus_2026-10-28.pdf"  # .pdf extension
        assert attached_name.endswith(".pdf")
    print("create_transaction_from_data PDF attachment tests passed!")


def test_split_payment_pdf_attachment():
    print("Testing split payment preserves PDF attachment format...")
    from app.receipt_processing import create_transaction_from_data

    attached_calls = []

    def mock_create_firefly_transaction(receipt, source_account, tags=None):
        tx_id = f"tx_{len(attached_calls) + 1}"
        journal_id = 700 + len(attached_calls)
        return {
            "data": {
                "id": tx_id,
                "attributes": {
                    "transactions": [{"transaction_journal_id": journal_id}]
                },
            }
        }

    def mock_attach(journal_id, file_bytes, filename):
        attached_calls.append((journal_id, file_bytes, filename))
        return True

    with patch(
        "app.receipt_processing.create_firefly_transaction",
        side_effect=mock_create_firefly_transaction,
    ), patch(
        "app.receipt_processing.attach_image_to_transaction",
        side_effect=mock_attach,
    ):
        receipt_data = {
            "date": "2026-10-28",
            "time": "14:35",
            "amount": 55.00,
            "store_name": "Spar",
            "description": "Split shopping",
            "category": "Groceries",
            "budget": "Monthly",
            "splits": [
                {"account": "Cash wallet", "amount": 20.0},
                {"account": "Easybank Giro", "amount": 35.0},
            ],
        }
        pdf_bytes = b"%PDF-1.4 split test pdf bytes"
        pdf_filename = "receipt_Spar_2026-10-28.pdf"

        res = asyncio.run(
            create_transaction_from_data(
                receipt_data,
                source_account="Cash wallet",
                image_bytes=pdf_bytes,
                filename=pdf_filename,
            )
        )

        assert "Split payment created" in res
        assert len(attached_calls) == 2
        for j_id, att_bytes, att_name in attached_calls:
            assert att_bytes == pdf_bytes  # Original PDF bytes
            assert att_name == "receipt_Spar_2026-10-28.pdf"  # .pdf extension
    print("Split payment PDF attachment tests passed!")


if __name__ == "__main__":
    test_detect_file_extension()
    test_build_attachment_filename()
    test_create_transaction_from_data_pdf_attachment()
    test_split_payment_pdf_attachment()
    print("\nAll attachment format tests passed successfully!")

