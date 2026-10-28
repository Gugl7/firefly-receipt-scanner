import base64
import json
import re
import time
from datetime import datetime
from functools import lru_cache

from fastapi import UploadFile
from google import genai
import httpx

from .config import Settings, get_settings
from .firefly import (
    attach_image_to_transaction,
    check_duplicate_transactions,
    create_firefly_transaction,
    get_firefly_budgets,
    get_firefly_categories,
)
from .image_utils import process_image
from .models import ReceiptModel


@lru_cache
def get_gemini_client() -> genai.Client:
    settings = get_settings()
    http_options = {}
    if settings.gemini_base_url:
        http_options["base_url"] = settings.gemini_base_url
    return genai.Client(
        api_key=settings.google_ai_api_key or "dummy",
        http_options=http_options or None,
    )


def extract_json_str(text: str) -> str:
    """Extract JSON object substring from raw model output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end >= start:
        return text[start : end + 1]
    return text


def parse_receipt_json(content: str) -> ReceiptModel:
    json_str = extract_json_str(content)
    return ReceiptModel.model_validate_json(json_str)


def normalize_time(time_str: str | None) -> str | None:
    """Normalize extracted time string into 24-hour HH:MM format."""
    if not time_str or not str(time_str).strip():
        return None
    raw = str(time_str).strip()
    if raw.lower() in ("null", "none", "unknown", "n/a", "--:--"):
        return None

    # Replace European 'h' or '.' separator e.g. '14h35'
    cleaned = raw.replace("h", ":").replace("H", ":").strip()

    formats = [
        "%H:%M",
        "%H:%M:%S",
        "%I:%M %p",
        "%I:%M:%S %p",
        "%I:%M%p",
        "%I:%M:%S%p",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%H:%M")
        except ValueError:
            continue

    # Regex search for HH:MM (with optional seconds and AM/PM)
    match = re.search(
        r"\b([01]?\d|2[0-3])[:.]([0-5]\d)(?:[:.]([0-5]\d))?\s*(am|pm|AM|PM)?\b",
        cleaned,
    )
    if match:
        h, m, _, meridian = match.groups()
        h_num = int(h)
        if meridian:
            if meridian.lower() == "pm" and h_num < 12:
                h_num += 12
            elif meridian.lower() == "am" and h_num == 12:
                h_num = 0
        return f"{h_num:02d}:{m}"

    return None


def extract_time_from_date_string(date_str: str) -> str | None:
    """Extract a time substring if the date string has a combined timestamp."""
    if not date_str:
        return None
    if "T" in date_str:
        return normalize_time(date_str.split("T", 1)[1])
    elif " " in date_str:
        parts = date_str.split(" ", 1)
        if len(parts) > 1:
            return normalize_time(parts[1])
    return None


def normalize_date(date_str: str) -> str:
    date_str = date_str.strip()
    # If date_str contains a time component e.g. 2026-10-28T14:35 or 2026-10-28 14:35
    if "T" in date_str:
        date_str = date_str.split("T", 1)[0].strip()
    elif " " in date_str and any(c in date_str.split(" ", 1)[0] for c in ("-", "/", ".")):
        date_str = date_str.split(" ", 1)[0].strip()

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d.%m.%Y",
        "%Y/%m/%d",
        "%d-%m-%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    print(f"Invalid date format: {date_str}. Using current date instead.")
    return datetime.now().strftime("%Y-%m-%d")


def _parse_router_response(response: httpx.Response) -> str:
    """Parse 9router response handling both standard JSON and SSE streams."""
    raw_text = response.text.strip()
    if not raw_text:
        raise ValueError(
            f"9router returned an empty response body (HTTP {response.status_code}). "
            "Please ensure the model supports multimodal/vision inputs and that 9router is responding."
        )

    # Check if the response is an SSE stream (Server-Sent Events)
    # Common when routing through Antigravity or stream-mandated models
    if (
        "text/event-stream" in response.headers.get("content-type", "")
        or raw_text.startswith("data:")
    ):
        print("9router returned a streaming (SSE) response, assembling chunks...")
        collected = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_part = line[5:].strip()
                if data_part == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data_part)
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        if "content" in delta and delta["content"]:
                            collected.append(delta["content"])
                        else:
                            msg = choices[0].get("message", {})
                            if "content" in msg and msg["content"]:
                                collected.append(msg["content"])
                except json.JSONDecodeError:
                    continue

        content = "".join(collected).strip()
        if not content:
            raise ValueError(
                f"9router returned a stream with no text content: {raw_text[:200]}"
            )
        return content

    # Standard JSON response
    try:
        data = response.json()
    except Exception as e:
        raise ValueError(
            f"Failed to parse 9router response as JSON (HTTP {response.status_code}): {raw_text[:200]}"
        ) from e

    choices = data.get("choices", [])
    if not choices or "message" not in choices[0]:
        raise ValueError(f"Invalid response format from 9router: {data}")

    content = choices[0]["message"].get("content", "")
    if not content:
        raise ValueError("9router response message content was empty.")
    return content


async def _extract_with_router(
    prompt: str, image_base64: str | None, settings: Settings
) -> ReceiptModel:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if settings.router_api_key:
        headers["Authorization"] = f"Bearer {settings.router_api_key}"

    user_content = [
        {
            "type": "text",
            "text": prompt,
        }
    ]
    if image_base64:
        user_content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{image_base64}",
                },
            }
        )

    payload = {
        "model": settings.effective_router_model,
        "messages": [
            {
                "role": "user",
                "content": user_content,
            }
        ],
        "response_format": {"type": "json_object"},
    }

    url = settings.router_chat_url
    print(
        f"Sending request to 9router at {url} (model: {settings.effective_router_model})..."
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            content = _parse_router_response(response)
        except (httpx.HTTPStatusError, ValueError) as e:
            should_retry_no_fmt = False
            if "response_format" in payload:
                if (
                    isinstance(e, httpx.HTTPStatusError)
                    and e.response.status_code == 400
                    and "response_format" in e.response.text.lower()
                ):
                    should_retry_no_fmt = True
                elif isinstance(e, ValueError) and "empty" in str(e).lower():
                    should_retry_no_fmt = True

            if should_retry_no_fmt:
                print(
                    "response_format not supported or produced empty output, retrying without it..."
                )
                payload.pop("response_format", None)
                try:
                    response = await client.post(url, headers=headers, json=payload)
                    response.raise_for_status()
                    content = _parse_router_response(response)
                except httpx.HTTPStatusError as retry_err:
                    err_text = retry_err.response.text
                    print(
                        f"9router HTTP error on retry: {retry_err.response.status_code} {err_text}"
                    )
                    raise RuntimeError(
                        f"9router HTTP {retry_err.response.status_code}: {err_text or retry_err.response.reason_phrase}"
                    ) from retry_err
            elif isinstance(e, httpx.HTTPStatusError):
                err_text = e.response.text
                print(f"9router HTTP error: {e.response.status_code} {err_text}")
                raise RuntimeError(
                    f"9router HTTP {e.response.status_code}: {err_text or e.response.reason_phrase}"
                ) from e
            else:
                raise
        except httpx.TimeoutException as e:
            raise TimeoutError(
                "The 9router request timed out. Please try again with a smaller or clearer image."
            ) from e

    print("Received response from 9router")
    return parse_receipt_json(content)


def build_extraction_prompt(
    categories: list[str], budgets: list[str], receipt_text: str | None = None
) -> str:
    target_desc = (
        "the provided receipt text" if receipt_text else "the attached receipt image"
    )
    prompt = (
        f"Please analyze {target_desc} and extract the following details:\n"
        "1) amount (receipt total amount as a number)\n"
        "2) category (choose the best matching category from: "
        + ", ".join(categories)
        + ")\n"
        "3) budget (choose the best matching budget from: "
        + ", ".join(budgets)
        + ")\n"
        "4) store_name (destination account / store or merchant name)\n"
        "5) description (brief description of the transaction)\n"
        "6) date (in YYYY-MM-DD format). Today's date is "
        + datetime.now().strftime("%Y-%m-%d")
        + ".\n"
        "Most receipts are from the past few days, so use today's date as a reference point when interpreting dates.\n"
        "If the date is not on the receipt, use today's date as the default.\n"
        "7) time (in 24-hour HH:MM format, e.g. '14:35'). If the exact time of purchase is printed on the receipt, extract it; otherwise set to null.\n\n"
    )
    if receipt_text:
        prompt += (
            "--- RECEIPT TEXT START ---\n"
            + receipt_text
            + "\n--- RECEIPT TEXT END ---\n\n"
        )
    prompt += (
        "Return ONLY a valid JSON object matching this schema:\n"
        "{\n"
        '  "date": "YYYY-MM-DD",\n'
        '  "time": "HH:MM",\n'
        '  "amount": 0.0,\n'
        '  "store_name": "string",\n'
        '  "description": "string",\n'
        '  "category": "string",\n'
        '  "budget": "string"\n'
        "}"
    )
    return prompt


async def _run_llm_extraction(
    prompt: str,
    image=None,
    image_base64: str | None = None,
    settings: Settings | None = None,
) -> ReceiptModel:
    if settings is None:
        settings = get_settings()

    if settings.effective_llm_provider == "9router":
        return await _extract_with_router(prompt, image_base64, settings)

    if not settings.google_ai_api_key:
        raise ValueError(
            "GOOGLE_AI_API_KEY is not set. Please set GOOGLE_AI_API_KEY in your .env or configure ROUTER_BASE_URL to use 9router."
        )
    try:
        print("Sending request to Gemini for analysis...")
        client = get_gemini_client()
        contents = [prompt]
        if image is not None:
            contents.append(image)

        gemini_response = client.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config={
                "response_mime_type": "application/json",
                "response_schema": ReceiptModel,
            },
        )
        print("Received response from Gemini")
        return gemini_response.parsed
    except Exception as e:
        print(f"Error during Gemini analysis: {str(e)}")
        print(f"Error type: {type(e)}")
        if "timeout" in str(e).lower():
            raise TimeoutError(
                "The image processing timed out. Please try again with a smaller or clearer image."
            )
        raise e


async def extract_receipt_data_from_text(receipt_text: str):
    """Extract receipt data from text without an image."""
    try:
        print("Processing receipt from text input...")
        categories = get_firefly_categories()
        budgets = get_firefly_budgets()

        if not categories:
            categories = [
                "Groceries",
                "Dining",
                "Shopping",
                "Transportation",
                "Entertainment",
                "Other",
            ]
            print("Using default categories due to Firefly III connection issues")

        if not budgets:
            budgets = ["Monthly", "Weekly", "Other"]
            print("Using default budgets due to Firefly III connection issues")

        prompt = build_extraction_prompt(categories, budgets, receipt_text=receipt_text)
        receipt = await _run_llm_extraction(prompt, image=None, image_base64=None)
        if not receipt.time:
            receipt.time = extract_time_from_date_string(receipt.date)
        receipt.date = normalize_date(receipt.date)
        receipt.time = normalize_time(receipt.time)

        return {
            "date": receipt.date,
            "time": receipt.time or "",
            "amount": receipt.amount,
            "store_name": receipt.store_name,
            "description": receipt.description,
            "category": receipt.category,
            "budget": receipt.budget,
            "available_categories": categories,
            "available_budgets": budgets,
            "image_base64": "",
        }
    except Exception as e:
        print(f"Unexpected error in extract_receipt_data_from_text: {str(e)}")
        raise


async def extract_receipt_data(
    file: UploadFile | bytes, filename: str | None = None
):
    """Extract data from the receipt image without creating a transaction."""
    try:
        fname = filename or (
            file.filename if isinstance(file, UploadFile) else "receipt.jpg"
        )
        print(f"Processing image: {fname}")

        # Process the image (resize and compress for sharp readability and fast LLM upload)
        image, image_bytes = await process_image(
            file, max_size=(1280, 1600), filename=fname
        )
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        print("Image processed and encoded to base64")

        # Fetch dynamic data from Firefly III
        print("Fetching categories and budgets...")
        categories = get_firefly_categories()
        budgets = get_firefly_budgets()
        print(
            f"Found {len(categories) if categories else 0} categories and {len(budgets) if budgets else 0} budgets"
        )

        # If we couldn't fetch categories or budgets, use default values
        if not categories:
            categories = [
                "Groceries",
                "Dining",
                "Shopping",
                "Transportation",
                "Entertainment",
                "Other",
            ]
            print("Using default categories due to Firefly III connection issues")

        if not budgets:
            budgets = ["Monthly", "Weekly", "Other"]
            print("Using default budgets due to Firefly III connection issues")

        prompt = build_extraction_prompt(categories, budgets)
        receipt = await _run_llm_extraction(
            prompt, image=image, image_base64=image_base64
        )

        # Validate and format the date and time
        if not receipt.time:
            receipt.time = extract_time_from_date_string(receipt.date)
        receipt.date = normalize_date(receipt.date)
        receipt.time = normalize_time(receipt.time)

        # Return the extracted data as a dictionary
        extracted_data = {
            "date": receipt.date,
            "time": receipt.time or "",
            "amount": receipt.amount,
            "store_name": receipt.store_name,
            "description": receipt.description,
            "category": receipt.category,
            "budget": receipt.budget,
            "available_categories": categories,
            "available_budgets": budgets,
            "image_base64": image_base64,
        }
        print("Successfully extracted all data")
        return extracted_data
    except Exception as e:
        print(f"Unexpected error in extract_receipt_data: {str(e)}")
        print(f"Error type: {type(e)}")
        raise


async def create_transaction_from_data(
    receipt_data, source_account, image_bytes=None, filename="receipt.jpg"
):
    """Create a transaction in Firefly III using the provided data."""
    # Create a ReceiptModel object from the data
    receipt = ReceiptModel(
        date=receipt_data["date"],
        time=receipt_data.get("time") or None,
        amount=receipt_data["amount"],
        store_name=receipt_data["store_name"],
        description=receipt_data["description"],
        category=receipt_data["category"],
        budget=receipt_data["budget"],
    )

    # Implement retry logic with exponential backoff
    max_retries = 3  # Reduced from 5 to 3 to prevent too many duplicates
    retry_delay = 3  # Keep at 3 seconds
    last_error = None

    for attempt in range(max_retries):
        try:
            # Create a transaction based on the receipt data
            transaction_result = create_firefly_transaction(receipt, source_account)

            if transaction_result:
                print("Transaction created successfully:")
                print(f"- Date: {receipt.date}")
                print(f"- Amount: {receipt.amount}")
                print(f"- Store: {receipt.store_name}")
                print(f"- Category: {receipt.category}")
                print(f"- Budget: {receipt.budget}")
                print(f"- Source Account: {source_account}")
                print(f"- Transaction ID: {transaction_result['data']['id']}")

                # Attach image if provided
                if image_bytes:
                    try:
                        journal_id = int(
                            transaction_result["data"]["attributes"][
                                "transactions"
                            ][0]["transaction_journal_id"]
                        )
                        attach_image_to_transaction(
                            journal_id, image_bytes, filename
                        )
                        print("Receipt image attached to transaction")
                    except Exception as e:
                        print(
                            f"Warning: Failed to attach receipt image: {e}"
                        )

                return f"Transaction created successfully with ID: {transaction_result['data']['id']}"
            else:
                last_error = (
                    "Failed to create transaction. No response from Firefly III."
                )
                print(last_error)

        except Exception as e:
            last_error = str(e)
            print(
                f"Error creating transaction (attempt {attempt + 1}/{max_retries}): {e}"
            )

            # Check if transaction was actually created despite error (e.g. timeout)
            try:
                possible_dups = check_duplicate_transactions(
                    receipt.date, receipt.amount, receipt.store_name, tolerance_days=0
                )
                exact_matches = [
                    d for d in possible_dups if d.get("confidence") == "high"
                ]
                if exact_matches:
                    dup_id = exact_matches[0]["id"]
                    print(
                        f"Transaction was already created in Firefly III (detected ID {dup_id})"
                    )
                    return f"Transaction created successfully with ID: {dup_id}"
            except Exception as dup_err:
                print(f"Warning: Duplicate check during retry failed: {dup_err}")

            # If this is not the last attempt, wait and retry
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2**attempt)  # Exponential backoff
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

    # If we've exhausted all retries, return an error message
    error_msg = f"Failed to create transaction after {max_retries} attempts. Last error: {last_error}"
    print(error_msg)
    return error_msg
