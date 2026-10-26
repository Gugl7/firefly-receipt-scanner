import io

from fastapi import UploadFile
from PIL import Image

try:
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None


async def process_image(file: UploadFile | bytes, max_size=(768, 768)):
    """
    Process an uploaded image or PDF by converting/resizing it and compressing to JPEG.

    Args:
        file: The uploaded file or raw image/PDF bytes
        max_size: Maximum dimensions (width, height) for the resized image

    Returns:
        A tuple containing (PIL.Image, resized_jpeg_bytes)
    """
    # Read the uploaded file or use bytes directly
    if isinstance(file, bytes):
        contents = file
    else:
        contents = await file.read()

    # Check if the content is a PDF (magic bytes %PDF-)
    if contents.startswith(b"%PDF-"):
        if pdfium is None:
            raise ImportError(
                "PDF processing requires 'pypdfium2'. Please install it or rebuild your Docker container."
            )
        pdf = pdfium.PdfDocument(contents)
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
        img = Image.open(io.BytesIO(contents))

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
