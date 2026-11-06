import asyncio
import base64
import io
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock external dependencies if not installed in host python
for mod in ["fastapi", "google", "google.genai", "httpx"]:
    try:
        __import__(mod)
    except (ImportError, AttributeError):
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

from PIL import Image
from app.config import Settings
from app.models import MultiReceiptModel, ReceiptModel
from app.ocr import (
    extract_digital_text_from_pdf,
    is_tesseract_installed,
    perform_tesseract_ocr,
    run_local_ocr,
)
from app.receipt_processing import extract_receipt_data


def test_ocr_settings():
    print("Testing OCR Settings configuration & aliases...")
    # 1. Defaults
    s1 = Settings(
        firefly_iii_url="http://firefly.test",
        firefly_iii_token="tok",
    )
    assert s1.ocr_enabled is False
    assert s1.ocr_engine == "tesseract"
    assert s1.ocr_language == "eng+deu"

    # 2. Configured via environment / constructor aliases
    s2 = Settings(
        firefly_iii_url="http://firefly.test",
        firefly_iii_token="tok",
        OCR_ENABLED=True,
        OCR_LANGUAGE="deu",
    )
    assert s2.ocr_enabled is True
    assert s2.ocr_language == "deu"

    s3 = Settings(
        firefly_iii_url="http://firefly.test",
        firefly_iii_token="tok",
        LOCAL_OCR_ENABLED="true",
        TESSERACT_LANG="eng",
    )
    assert s3.ocr_enabled is True
    assert s3.ocr_language == "eng"
    print("OCR Settings tests passed!")


def test_perform_tesseract_ocr_mock():
    print("Testing perform_tesseract_ocr with mock...")
    img = Image.new("RGB", (100, 100), color="white")

    # Mock pytesseract returning text
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "BILLA PLUS\nTotal: 45.90 EUR\nDatum: 2026-10-28"

    with patch("app.ocr.pytesseract", mock_pytesseract):
        text = perform_tesseract_ocr(img, language="eng+deu")
        assert "BILLA PLUS" in text
        assert "45.90" in text

    # Mock direct CLI subprocess when pytesseract is None
    mock_sub = MagicMock()
    mock_sub.return_code = 0
    mock_sub.stdout = "SPAR SUPERMARKT\nTotal: 18.50 EUR"
    mock_sub.returncode = 0

    with patch("app.ocr.pytesseract", None), patch(
        "app.ocr.shutil.which", return_value="/usr/bin/tesseract"
    ), patch("app.ocr.subprocess.run", return_value=mock_sub):
        text = perform_tesseract_ocr(img, language="eng+deu")
        assert "SPAR SUPERMARKT" in text

    print("perform_tesseract_ocr mock tests passed!")


def test_extract_digital_text_from_pdf():
    print("Testing extract_digital_text_from_pdf...")
    # Mock pdfium
    mock_pdfium = MagicMock()
    mock_page = MagicMock()
    mock_textpage = MagicMock()
    mock_textpage.get_text_range.return_value = "INVOICE #12345\nMerchant: Amazon EU S.a.r.l.\nTotal: 129.00 EUR\nDate: 2026-10-25"
    mock_page.get_textpage.return_value = mock_textpage
    mock_doc = [mock_page]
    mock_pdfium.PdfDocument.return_value = mock_doc

    with patch("app.ocr.pdfium", mock_pdfium):
        res = extract_digital_text_from_pdf(b"%PDF-1.4 dummy content")
        assert res is not None
        assert "INVOICE #12345" in res

    print("extract_digital_text_from_pdf tests passed!")


def test_extract_receipt_data_with_ocr():
    print("Testing extract_receipt_data with OCR enabled vs fallback...")
    dummy_img = Image.new("RGB", (200, 200), color="white")
    buf = io.BytesIO()
    dummy_img.save(buf, format="JPEG")
    jpeg_bytes = buf.getvalue()

    mock_receipt = ReceiptModel(
        date="2026-10-28",
        amount=32.50,
        store_name="Billa Plus",
        description="Groceries from OCR",
        category="Groceries",
        budget="Monthly",
    )
    mock_multi = MultiReceiptModel(transactions=[mock_receipt])

    # Case A: Local OCR is enabled and succeeds -> sends text prompt (image=None)
    with patch(
        "app.receipt_processing.run_local_ocr",
        return_value="Billa Plus\nTotal: EUR 32.50\nDate: 2026-10-28",
    ), patch(
        "app.receipt_processing._run_llm_extraction",
        new=AsyncMock(return_value=mock_multi),
    ) as mock_llm, patch(
        "app.receipt_processing.get_firefly_categories",
        return_value=["Groceries", "Dining"],
    ), patch(
        "app.receipt_processing.get_firefly_budgets",
        return_value=["Monthly"],
    ):
        res = asyncio.run(
            extract_receipt_data(
                jpeg_bytes,
                filename="receipt.jpg",
                use_ocr=True,
            )
        )
        assert res["store_name"] == "Billa Plus"
        assert res["amount"] == 32.50
        assert res["category"] == "Groceries"
        assert res["image_base64"] != ""  # Preview is still preserved!

        # Verify LLM was called with text prompt only (image=None, image_base64=None)
        _, kwargs = mock_llm.call_args
        assert kwargs.get("image") is None
        assert kwargs.get("image_base64") is None

    # Case B: Local OCR returns None / fails -> falls back to vision (image is provided)
    with patch(
        "app.receipt_processing.run_local_ocr",
        return_value=None,
    ), patch(
        "app.receipt_processing._run_llm_extraction",
        new=AsyncMock(return_value=mock_receipt),
    ) as mock_llm, patch(
        "app.receipt_processing.get_firefly_categories",
        return_value=["Groceries", "Dining"],
    ), patch(
        "app.receipt_processing.get_firefly_budgets",
        return_value=["Monthly"],
    ):
        res = asyncio.run(
            extract_receipt_data(
                jpeg_bytes,
                filename="receipt.jpg",
                use_ocr=True,
            )
        )
        assert res["store_name"] == "Billa Plus"
        _, kwargs = mock_llm.call_args
        # In fallback, visual image is passed to LLM
        assert kwargs.get("image") is not None
        assert kwargs.get("image_base64") is not None

    print("extract_receipt_data OCR and fallback tests passed!")


if __name__ == "__main__":
    test_ocr_settings()
    test_perform_tesseract_ocr_mock()
    test_extract_digital_text_from_pdf()
    test_extract_receipt_data_with_ocr()
    print("\nAll OCR unit tests passed successfully!")

