import os
import shutil
import subprocess
import tempfile
from typing import Optional

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None

try:
    import pytesseract
except ImportError:
    pytesseract = None


def is_tesseract_installed() -> bool:
    """Check if the tesseract CLI binary is available on the system."""
    return shutil.which("tesseract") is not None


def extract_digital_text_from_pdf(pdf_bytes: bytes) -> Optional[str]:
    """
    Extract digital / embedded text directly from a PDF without OCR if available.
    Returns combined text string if sufficient text is found, else None.
    """
    if pdfium is None or not pdf_bytes:
        return None

    try:
        pdf_idx = pdf_bytes[:1024].find(b"%PDF-")
        clean_bytes = pdf_bytes[pdf_idx:] if pdf_idx > 0 else pdf_bytes
        doc = pdfium.PdfDocument(clean_bytes)

        pages_text = []
        for page in doc:
            textpage = page.get_textpage()
            txt = textpage.get_text_range()
            if txt and txt.strip():
                pages_text.append(txt.strip())

        combined = "\n\n".join(pages_text).strip()
        # If we got at least 40 characters of digital text, use it
        if len(combined) >= 40:
            print(f"Extracted {len(combined)} characters of digital text directly from PDF.")
            return combined
    except Exception as e:
        print(f"Note: Could not extract digital text from PDF: {e}")

    return None


def perform_tesseract_ocr(
    image,
    language: str = "eng+deu",
) -> Optional[str]:
    """
    Run Tesseract OCR on a PIL Image.
    Prefers pytesseract if installed, otherwise calls tesseract CLI directly.
    """
    if image is None:
        return None

    # 1. Try pytesseract if available
    if pytesseract is not None:
        try:
            text = pytesseract.image_to_string(image, lang=language)
            cleaned = text.strip() if text else ""
            if cleaned:
                return cleaned
        except Exception as e:
            print(f"pytesseract error: {e}")
            # If language not found (e.g. deu missing), fallback to eng
            if "deu" in language:
                try:
                    text = pytesseract.image_to_string(image, lang="eng")
                    if text and text.strip():
                        return text.strip()
                except Exception:
                    pass

    # 2. Fallback: invoke tesseract CLI directly via subprocess
    tess_bin = shutil.which("tesseract")
    if tess_bin:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                image.save(tmp.name, format="PNG")
                tmp_path = tmp.name

            # Try requested language first
            cmd = [tess_bin, tmp_path, "stdout", "-l", language]
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20
            )

            # If failed because language pack is missing, retry with 'eng'
            if res.returncode != 0 and "deu" in language:
                cmd = [tess_bin, tmp_path, "stdout", "-l", "eng"]
                res = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=20
                )

            if res.returncode == 0:
                out = res.stdout.strip()
                if out:
                    return out
            else:
                print(f"Tesseract CLI returned error {res.returncode}: {res.stderr[:200]}")
        except Exception as e:
            print(f"Tesseract CLI execution error: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    return None


def run_local_ocr(
    image,
    raw_bytes: Optional[bytes] = None,
    is_pdf: bool = False,
    language: str = "eng+deu",
) -> Optional[str]:
    """
    Main entry point for local text extraction.
    1. If PDF: tries direct digital text extraction first.
    2. If no digital text or image: runs Tesseract OCR.
    Returns cleaned text string or None if OCR is unavailable/empty.
    """
    # Step 1: Check for digital PDF text
    if is_pdf and raw_bytes:
        digital_text = extract_digital_text_from_pdf(raw_bytes)
        if digital_text:
            return digital_text

    # Step 2: Run Tesseract on the rendered/uploaded image
    return perform_tesseract_ocr(image, language=language)

