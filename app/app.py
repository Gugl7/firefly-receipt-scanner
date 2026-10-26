import base64
import os
import re
import shutil
import sys
import tempfile
import time
import uuid

from fastapi import (
    FastAPI,
    File,
    Form,
    Request,
    UploadFile,
)
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .firefly import get_firefly_asset_accounts, get_firefly_categories
from .receipt_processing import create_transaction_from_data, extract_receipt_data


def sanitize_filename(name: str) -> str:
    """Make a string safe to use as an attachment filename."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return name[:50] or "receipt"


def cleanup_old_batches(max_age_seconds: int = 7200):
    """Remove batch directories older than max_age_seconds (default 2 hours)."""
    base_dir = os.path.join(tempfile.gettempdir(), "receipt_scanner")
    if not os.path.exists(base_dir):
        return
    now = time.time()
    for entry in os.scandir(base_dir):
        if entry.is_dir():
            try:
                if now - entry.stat().st_mtime > max_age_seconds:
                    shutil.rmtree(entry.path, ignore_errors=True)
            except Exception:
                pass


def test_firefly_connection():
    """Test the connection to Firefly III by attempting to fetch categories."""
    try:
        categories = get_firefly_categories()
        if categories is None:
            print("Error: Could not connect to Firefly III. Categories returned None.")
            return False
        print("Successfully connected to Firefly III")
        return True
    except Exception as e:
        print(f"Error connecting to Firefly III: {str(e)}")
        return False


app = FastAPI(title="Receipt to Firefly III")

# Add ProxyHeadersMiddleware to trust X-Forwarded-Proto and X-Forwarded-Host from reverse proxy
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Test Firefly III connection before proceeding
if not test_firefly_connection():
    print("Failed to establish connection to Firefly III. Exiting...")
    sys.exit(1)

# Add trusted host middleware to handle forwarded headers
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

# Create a static directory if it doesn't exist
os.makedirs("app/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Set up templates
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request, cancel_batch: str | None = None):
    """Serve a simple HTML form for uploading receipts."""
    if cancel_batch:
        batch_dir = os.path.join(tempfile.gettempdir(), "receipt_scanner", cancel_batch)
        shutil.rmtree(batch_dir, ignore_errors=True)
        print(f"Batch {cancel_batch} cancelled and cleaned up.")

    # Fetch asset accounts from Firefly III
    asset_accounts = get_firefly_asset_accounts()

    # If no accounts were fetched, use a default
    if not asset_accounts:
        asset_accounts = ["Cash wallet"]
        print("Using default asset account due to Firefly III connection issues")

    return templates.TemplateResponse(
        "upload.html", {"request": request, "asset_accounts": asset_accounts}
    )


@app.post("/extract")
async def extract_receipt(
    request: Request,
    source_account: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    file: UploadFile | None = File(default=None),
):
    try:
        cleanup_old_batches()

        # Support both multiple files ('files') and single file ('file') inputs
        all_files = [f for f in files if f.filename]
        if file and file.filename and file not in all_files:
            all_files.append(file)

        if not all_files:
            return templates.TemplateResponse(
                "error.html",
                {
                    "request": request,
                    "error_message": "Please select at least one receipt image to upload.",
                },
            )

        # Create batch session directory to store remaining images
        batch_id = str(uuid.uuid4())
        batch_dir = os.path.join(tempfile.gettempdir(), "receipt_scanner", batch_id)
        os.makedirs(batch_dir, exist_ok=True)

        valid_count = 0
        first_bytes = None
        first_filename = None

        for idx, upload_file in enumerate(all_files):
            contents = await upload_file.read()
            if contents:
                target_path = os.path.join(batch_dir, f"{valid_count}.dat")
                with open(target_path, "wb") as f_out:
                    f_out.write(contents)
                name_path = os.path.join(batch_dir, f"{valid_count}.name")
                with open(name_path, "w") as f_name:
                    f_name.write(upload_file.filename or "")
                if valid_count == 0:
                    first_bytes = contents
                    first_filename = upload_file.filename
                valid_count += 1

        if valid_count == 0 or not first_bytes:
            shutil.rmtree(batch_dir, ignore_errors=True)
            return templates.TemplateResponse(
                "error.html",
                {
                    "request": request,
                    "error_message": "Uploaded files were empty. Please try again with valid receipt images.",
                },
            )

        print(
            f"Created batch {batch_id} with {valid_count} receipts. Processing receipt 1 of {valid_count}..."
        )

        # Process the first image
        extracted_data = await extract_receipt_data(
            first_bytes, filename=first_filename or "receipt_1.jpg"
        )
        extracted_data["source_account"] = source_account

        asset_accounts = get_firefly_asset_accounts() or [source_account]

        return templates.TemplateResponse(
            "review.html",
            {
                "request": request,
                "extracted_data": extracted_data,
                "batch_id": batch_id,
                "current_index": 0,
                "batch_total": valid_count,
                "created_count": 0,
                "asset_accounts": asset_accounts,
            },
        )
    except TimeoutError:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_message": "The operation timed out. The image processing is taking too long. Please try again with a smaller or clearer image.",
            },
        )
    except Exception as e:
        return templates.TemplateResponse(
            "error.html", {"request": request, "error_message": str(e)}
        )


@app.post("/create-transaction")
async def create_transaction(
    request: Request,
    date: str = Form(...),
    amount: float = Form(...),
    store_name: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    budget: str = Form(...),
    source_account: str = Form(...),
    attach_receipt: str = Form(""),
    image_base64: str = Form(""),
    batch_id: str = Form(""),
    current_index: int = Form(0),
    batch_total: int = Form(1),
    created_count: int = Form(0),
    action: str = Form("create"),
):
    try:
        # 1. Process current receipt (create transaction or skip)
        if action == "create":
            # Decode image bytes if attachment is requested
            image_bytes = None
            if attach_receipt and image_base64:
                try:
                    image_bytes = base64.b64decode(image_base64)
                except Exception as e:
                    print(f"Warning: Failed to decode image for attachment: {e}")

            # Create the transaction in Firefly III
            result = await create_transaction_from_data(
                {
                    "date": date,
                    "amount": amount,
                    "store_name": store_name,
                    "description": description,
                    "category": category,
                    "budget": budget,
                    "source_account": source_account,
                },
                source_account,
                image_bytes=image_bytes,
                filename=f"receipt_{sanitize_filename(store_name)}_{sanitize_filename(date)}.jpg",
            )

            if result and "Failed to create transaction" in result:
                return templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error_message": result,
                        "batch_id": batch_id,
                        "current_index": current_index,
                        "batch_total": batch_total,
                        "created_count": created_count,
                        "source_account": source_account,
                    },
                )
            created_count += 1
        else:
            print(f"Skipped receipt {current_index + 1} of {batch_total}")

        # 2. Check if there are more receipts in the batch
        next_index = current_index + 1
        if batch_id and next_index < batch_total:
            batch_dir = os.path.join(tempfile.gettempdir(), "receipt_scanner", batch_id)
            next_file_path = os.path.join(batch_dir, f"{next_index}.dat")
            if os.path.exists(next_file_path):
                print(
                    f"Processing next batch receipt {next_index + 1} of {batch_total}..."
                )
                try:
                    with open(next_file_path, "rb") as f_in:
                        next_bytes = f_in.read()

                    next_filename = None
                    name_path = os.path.join(batch_dir, f"{next_index}.name")
                    if os.path.exists(name_path):
                        with open(name_path, "r") as f_name:
                            next_filename = f_name.read().strip() or None

                    next_extracted_data = await extract_receipt_data(
                        next_bytes, filename=next_filename or f"receipt_{next_index + 1}.jpg"
                    )
                    next_extracted_data["source_account"] = source_account

                    asset_accounts = get_firefly_asset_accounts() or [source_account]

                    return templates.TemplateResponse(
                        "review.html",
                        {
                            "request": request,
                            "extracted_data": next_extracted_data,
                            "batch_id": batch_id,
                            "current_index": next_index,
                            "batch_total": batch_total,
                            "created_count": created_count,
                            "asset_accounts": asset_accounts,
                        },
                    )
                except Exception as e:
                    print(
                        f"Error extracting data for receipt {next_index + 1}: {e}"
                    )
                    return templates.TemplateResponse(
                        "error.html",
                        {
                            "request": request,
                            "error_message": f"Error parsing receipt {next_index + 1} of {batch_total}: {str(e)}",
                            "batch_id": batch_id,
                            "current_index": next_index,
                            "batch_total": batch_total,
                            "created_count": created_count,
                            "source_account": source_account,
                        },
                    )

        # 3. All receipts in the batch have been processed: clean up
        if batch_id:
            batch_dir = os.path.join(tempfile.gettempdir(), "receipt_scanner", batch_id)
            shutil.rmtree(batch_dir, ignore_errors=True)

        if created_count > 1:
            success_msg = f"All {created_count} transactions created successfully!"
        elif created_count == 1:
            success_msg = "Transaction created successfully!"
        else:
            success_msg = "Batch processing finished (no transactions were created)."

        return templates.TemplateResponse(
            "upload.html",
            {
                "request": request,
                "asset_accounts": get_firefly_asset_accounts(),
                "success_message": success_msg,
            },
        )
    except Exception as e:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_message": str(e),
                "batch_id": batch_id,
                "current_index": current_index,
                "batch_total": batch_total,
                "created_count": created_count,
                "source_account": source_account,
            },
        )
