import io

from fastapi import UploadFile
from PIL import Image

try:
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None


async def process_image(
    file: UploadFile | bytes,
    max_size: tuple[int, int] = (768, 768),
    filename: str | None = None,
):
    """
    Process an uploaded image or PDF by converting/resizing it and compressing to JPEG.

    Args:
        file: The uploaded file or raw image/PDF bytes
        max_size: Maximum dimensions (width, height) for the resized image
        filename: Optional filename to assist in format detection

    Returns:
        A tuple containing (PIL.Image, resized_jpeg_bytes)
    """
    # Read the uploaded file or use bytes directly
    if isinstance(file, bytes):
        contents = file
        fname = filename or ""
    else:
        contents = await file.read()
        fname = filename or getattr(file, "filename", "") or ""

    # Check if the content is a PDF:
    # 1. Signature anywhere in the first 1024 bytes (per PDF spec ISO 32000-1)
    # 2. Or filename ending in .pdf
    # 3. Or content_type header 'application/pdf'
    pdf_idx = contents[:1024].find(b"%PDF-")
    is_pdf = (
        pdf_idx != -1
        or fname.lower().endswith(".pdf")
        or (hasattr(file, "content_type") and getattr(file, "content_type", "") == "application/pdf")
    )

    if is_pdf:
        if pdfium is None:
            raise RuntimeError(
                "PDF receipt uploaded, but 'pypdfium2' is not installed in the container environment. "
                "Please rebuild the container by running: docker-compose up -d --build"
            )
        # Ensure buffer begins at %PDF- marker if there was leading garbage/BOM
        pdf_data = contents[pdf_idx:] if pdf_idx > 0 else contents
        try:
            pdf = pdfium.PdfDocument(pdf_data)
        except Exception as e:
            raise ValueError(f"Failed to open PDF document: {e}") from e

        if len(pdf) == 0:
            raise ValueError("The uploaded PDF contains no pages.")

        if len(pdf) == 1:
            img = pdf[0].render(scale=2).to_pil()
        else:
            # Multi-page PDF: render up to 3 pages and stitch vertically
            max_pages = min(len(pdf), 3)
            rendered = [pdf[i].render(scale=2).to_pil() for i in range(max_pages)]
            total_w = max(p.width for p in rendered)
            total_h = sum(p.height for p in rendered)
            stitched = Image.new("RGB", (total_w, total_h), (255, 255, 255))
            y = 0
            for p in rendered:
                stitched.paste(p, (0, y))
                y += p.height
            img = stitched
    else:
        # Open the image using PIL
        try:
            img = Image.open(io.BytesIO(contents))
        except Exception as e:
            raise ValueError(
                f"Could not identify image or document format for '{fname or 'receipt'}'. "
                "Please ensure you are uploading a valid image (JPEG, PNG, WebP) or PDF receipt."
            ) from e

    # Convert to RGB if necessary (in case of RGBA or other formats)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Resize the image while maintaining aspect ratio
    img.thumbnail(max_size, Image.LANCZOS)

    # Re-encode the resized image so callers get a small JPEG payload
    # instead of the original, potentially multi-MB upload
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=85)

    return img, buffer.getvalue()
