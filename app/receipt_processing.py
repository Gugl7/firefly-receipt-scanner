import asyncio
import base64
import json
import re
import time
from datetime import datetime
from functools import lru_cache

from fastapi import UploadFile
from google import genai
import httpx

from .account_matching import clean_card_digits
from .config import Settings, get_settings
from .firefly import (
    attach_image_to_transaction,
    check_duplicate_transactions,
    create_firefly_transaction,
    get_firefly_budgets,
    get_firefly_categories,
)
from .image_utils import process_image
from .ocr import run_local_ocr
from .models import (
    CsvRefineResponse,
    CsvTransactionUpdate,
    MultiReceiptModel,
    ReceiptModel,
    validate_model_json,
    validate_model_obj,
)


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
    """Extract JSON object or array substring from raw model output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    first_brace = text.find("{")
    first_bracket = text.find("[")

    if first_bracket != -1 and (first_brace == -1 or first_bracket < first_brace):
        last_bracket = text.rfind("]")
        if last_bracket != -1 and last_bracket >= first_bracket:
            return text[first_bracket : last_bracket + 1]

    if first_brace != -1:
        last_brace = text.rfind("}")
        if last_brace != -1 and last_brace >= first_brace:
            return text[first_brace : last_brace + 1]

    return text


def parse_receipt_json(content: str) -> ReceiptModel:
    json_str = extract_json_str(content)
    return validate_model_json(ReceiptModel, json_str)


def parse_multi_receipt_json(content: str) -> MultiReceiptModel:
    """Parse JSON content from LLM into a MultiReceiptModel containing one or more transactions."""
    json_str = extract_json_str(content)
    try:
        data = json.loads(json_str)
    except Exception:
        # Fallback to single receipt parsing if it parses as a single receipt
        single = parse_receipt_json(content)
        return MultiReceiptModel(transactions=[single])

    if isinstance(data, list):
        tx_list = [
            validate_model_obj(ReceiptModel, item)
            for item in data
            if isinstance(item, dict)
        ]
        return MultiReceiptModel(transactions=tx_list)

    if isinstance(data, dict):
        if "transactions" in data and isinstance(data["transactions"], list):
            tx_list = [
                validate_model_obj(ReceiptModel, item)
                for item in data["transactions"]
                if isinstance(item, dict)
            ]
            return MultiReceiptModel(transactions=tx_list)
        if "receipts" in data and isinstance(data["receipts"], list):
            tx_list = [
                validate_model_obj(ReceiptModel, item)
                for item in data["receipts"]
                if isinstance(item, dict)
            ]
            return MultiReceiptModel(transactions=tx_list)
        # Single receipt object dictionary
        return MultiReceiptModel(transactions=[validate_model_obj(ReceiptModel, data)])

    return MultiReceiptModel(transactions=[])


def parse_csv_refine_json(content: str) -> CsvRefineResponse:
    json_str = extract_json_str(content)
    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            items = [
                validate_model_obj(CsvTransactionUpdate, x)
                for x in data
                if isinstance(x, dict)
            ]
            return CsvRefineResponse(updates=items)
        if isinstance(data, dict):
            for k in ("updates", "transactions", "items", "data"):
                if k in data and isinstance(data[k], list):
                    items = [
                        validate_model_obj(CsvTransactionUpdate, x)
                        for x in data[k]
                        if isinstance(x, dict)
                    ]
                    return CsvRefineResponse(updates=items)
    except Exception as e:
        print(f"Warning: Failed to parse CSV refine JSON directly: {e}")
    try:
        return validate_model_json(CsvRefineResponse, json_str)
    except Exception:
        return CsvRefineResponse(updates=[])



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
    prompt: str,
    image_base64: str | None,
    settings: Settings,
    response_model=ReceiptModel,
) -> ReceiptModel | MultiReceiptModel:
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
    timeout_secs = float(getattr(settings, "llm_timeout", 300.0))
    print(
        f"Sending request to 9router at {url} (model: {settings.effective_router_model}, timeout: {timeout_secs:.0f}s)..."
    )
    async with httpx.AsyncClient(timeout=timeout_secs) as client:
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
                f"The model request timed out after {timeout_secs:.0f} seconds. "
                "If your model is running on CPU or loading into memory, you can increase LLM_TIMEOUT in your .env (e.g. LLM_TIMEOUT=600)."
            ) from e

    print("Received response from 9router")
    if response_model == MultiReceiptModel:
        return parse_multi_receipt_json(content)
    if response_model == CsvRefineResponse:
        return parse_csv_refine_json(content)
    return parse_receipt_json(content)


def build_extraction_prompt(
    categories: list[str],
    budgets: list[str],
    receipt_text: str | None = None,
    system_prompt: str | None = None,
) -> str:
    today_str = datetime.now().strftime("%Y-%m-%d")

    custom_instructions = ""
    if system_prompt and system_prompt.strip():
        custom_instructions = (
            "--- USER CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---\n"
            f"{system_prompt.strip()}\n"
            "Pay close attention to these instructions and follow them when extracting the transaction details.\n"
            "------------------------------------------------\n\n"
        )

    if receipt_text:
        prompt = (
            custom_instructions
            + "Please analyze the provided text and extract all transactions found in it.\n"
            "The text may contain a SINGLE transaction or MULTIPLE distinct transactions (e.g. an expense list, multiple receipts, or pasted statement lines).\n"
            "For each transaction found, extract the following details:\n"
            "1) amount (receipt total amount as a number)\n"
            "2) category (choose the best matching category from: "
            + ", ".join(categories)
            + ", or null if no category applies or none is needed)\n"
            "3) budget (choose the best matching budget from: "
            + (", ".join(budgets) if budgets else "None")
            + ", or null if no budget applies or none is needed)\n"
            "4) store_name (destination account / store or merchant name)\n"
            "5) description (brief description of the transaction)\n"
            "6) date (in YYYY-MM-DD format). Today's date is "
            + today_str
            + ".\n"
            "Most transactions are from the past few days, so use today's date as a reference point when interpreting dates.\n"
            "If the date is not specified, use today's date as the default.\n"
            "7) time (in 24-hour HH:MM format, e.g. '14:35'). If the time of purchase is specified, extract it; otherwise set to null.\n"
            "8) payment_method ('card', 'cash', 'split', or 'other'). Look for card payment keywords (e.g. Visa, Mastercard, Maestro, EC, Debit, Credit, Kartenzahlung) or cash payment keywords (e.g. Cash, Bar, Barzahlung, Geg. Bar, Wechselgeld). If paid with multiple methods, specify 'split'.\n"
            "9) card_last_four (the last 4 digits of the payment card if mentioned, e.g. '1234', '**** 1234', '...1234'). If paid in cash or card digits are not mentioned, set to null.\n"
            "10) transaction_type ('withdrawal', 'transfer', or 'deposit'). Default is 'withdrawal'. If this transaction represents an ATM cash withdrawal or a transfer between accounts, specify 'transfer'.\n"
            "11) destination_account (if transaction_type is 'transfer', the receiving asset account name e.g. 'Cash wallet' or 'Savings'; otherwise null).\n"
            "12) is_split_payment (boolean, true if the transaction was paid with multiple payment methods; otherwise false).\n"
            "13) splits (if is_split_payment is true, list of split payment objects with 'payment_method', 'amount', and optional 'card_last_four'; otherwise empty list []).\n\n"
            "--- RECEIPT TEXT START ---\n"
            + receipt_text
            + "\n--- RECEIPT TEXT END ---\n\n"
            "Return ONLY a valid JSON object with a 'transactions' array containing all extracted transactions (even if only 1 transaction is found):\n"
            "{\n"
            '  "transactions": [\n'
            "    {\n"
            '      "date": "YYYY-MM-DD",\n'
            '      "time": "HH:MM",\n'
            '      "amount": 0.0,\n'
            '      "store_name": "string",\n'
            '      "description": "string",\n'
            '      "category": "string | null",\n'
            '      "budget": "string | null",\n'
            '      "payment_method": "card | cash | split | other | null",\n'
            '      "card_last_four": "1234 | null",\n'
            '      "transaction_type": "withdrawal | transfer | deposit",\n'
            '      "destination_account": "string | null",\n'
            '      "is_split_payment": false,\n'
            '      "splits": []\n'
            "    }\n"
            "  ]\n"
            "}"
        )
        return prompt

    target_desc = "the attached receipt image"
    prompt = (
        custom_instructions
        + f"Please analyze {target_desc} and extract the following details:\n"
        "1) amount (receipt total amount as a number)\n"
        "2) category (choose the best matching category from: "
        + ", ".join(categories)
        + ", or null if no category applies or none is needed)\n"
        "3) budget (choose the best matching budget from: "
        + (", ".join(budgets) if budgets else "None")
        + ", or null if no budget applies or none is needed)\n"
        "4) store_name (destination account / store or merchant name)\n"
        "5) description (brief description of the transaction)\n"
        "6) date (in YYYY-MM-DD format). Today's date is "
        + today_str
        + ".\n"
        "Most receipts are from the past few days, so use today's date as a reference point when interpreting dates.\n"
        "If the date is not on the receipt, use today's date as the default.\n"
        "7) time (in 24-hour HH:MM format, e.g. '14:35'). If the exact time of purchase is printed on the receipt, extract it; otherwise set to null.\n"
        "8) payment_method ('card', 'cash', 'split', or 'other'). Look for card payment keywords (e.g. Visa, Mastercard, Maestro, EC, Debit, Credit, Kartenzahlung) or cash payment keywords (e.g. Cash, Bar, Barzahlung, Geg. Bar, Wechselgeld). If paid with multiple methods, specify 'split'.\n"
        "9) card_last_four (the last 4 digits of the payment card if printed on the receipt, e.g. '1234', '**** 1234', '...1234', 'PAN: *******1234'). If paid in cash or card digits are not printed, set to null.\n"
        "10) transaction_type ('withdrawal', 'transfer', or 'deposit'). Default is 'withdrawal'. If this receipt represents an ATM cash withdrawal or a transfer between accounts, specify 'transfer'.\n"
        "11) destination_account (if transaction_type is 'transfer', the receiving asset account name e.g. 'Cash wallet' or 'Savings'; otherwise null).\n"
        "12) is_split_payment (boolean, true if the receipt was paid with multiple payment methods, e.g. part in cash and part with card; otherwise false).\n"
        "13) splits (if is_split_payment is true, list of split payment objects with 'payment_method', 'amount', and optional 'card_last_four'; otherwise empty list []).\n\n"
        "Return ONLY a valid JSON object matching this schema:\n"
        "{\n"
        '  "date": "YYYY-MM-DD",\n'
        '  "time": "HH:MM",\n'
        '  "amount": 0.0,\n'
        '  "store_name": "string",\n'
        '  "description": "string",\n'
        '  "category": "string | null",\n'
        '  "budget": "string | null",\n'
        '  "payment_method": "card | cash | split | other | null",\n'
        '  "card_last_four": "1234 | null",\n'
        '  "transaction_type": "withdrawal | transfer | deposit",\n'
        '  "destination_account": "string | null",\n'
        '  "is_split_payment": false,\n'
        '  "splits": [\n'
        '    {"payment_method": "cash", "amount": 0.0, "card_last_four": null},\n'
        '    {"payment_method": "card", "amount": 0.0, "card_last_four": "1234"}\n'
        '  ]\n'
        "}"
    )
    return prompt


async def _run_llm_extraction(
    prompt: str,
    image=None,
    image_base64: str | None = None,
    settings: Settings | None = None,
    response_model=ReceiptModel,
) -> ReceiptModel | MultiReceiptModel:
    if settings is None:
        settings = get_settings()

    if settings.effective_llm_provider == "9router":
        return await _extract_with_router(
            prompt, image_base64, settings, response_model=response_model
        )

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
                "response_schema": response_model,
            },
        )
        print("Received response from Gemini")
        parsed = gemini_response.parsed
        if parsed is None and hasattr(gemini_response, "text") and gemini_response.text:
            if response_model == MultiReceiptModel:
                return parse_multi_receipt_json(gemini_response.text)
            if response_model == CsvRefineResponse:
                return parse_csv_refine_json(gemini_response.text)
            return parse_receipt_json(gemini_response.text)
        return parsed
    except Exception as e:
        print(f"Error during Gemini analysis: {str(e)}")
        print(f"Error type: {type(e)}")
        if "timeout" in str(e).lower():
            raise TimeoutError(
                "The image processing timed out. Please try again with a smaller or clearer image."
            )
        raise e


async def extract_receipt_data_from_text(
    receipt_text: str,
    system_prompt: str | None = None,
) -> list[dict]:
    """Extract receipt data from text without an image, supporting single or multiple transactions."""
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

        prompt = build_extraction_prompt(
            categories, budgets, receipt_text=receipt_text, system_prompt=system_prompt
        )
        result = await _run_llm_extraction(
            prompt, image=None, image_base64=None, response_model=MultiReceiptModel
        )

        raw_txs: list[ReceiptModel] = []
        if isinstance(result, MultiReceiptModel):
            raw_txs = result.transactions
        elif isinstance(result, ReceiptModel):
            raw_txs = [result]
        elif isinstance(result, list):
            raw_txs = result

        if not raw_txs:
            raw_txs = [
                ReceiptModel(
                    date=datetime.now().strftime("%Y-%m-%d"),
                    time=None,
                    amount=0.0,
                    store_name="Unknown Store",
                    description=receipt_text[:100].strip() or "Pasted transaction",
                    category=categories[0] if categories else "Groceries",
                    budget=budgets[0] if budgets else "Monthly",
                )
            ]

        extracted_list = []
        for receipt in raw_txs:
            if not receipt.time:
                receipt.time = extract_time_from_date_string(receipt.date)
            receipt.date = normalize_date(receipt.date)
            receipt.time = normalize_time(receipt.time)
            clean_card = clean_card_digits(receipt.card_last_four)

            # Parse splits if split payment
            parsed_splits = []
            is_split = bool(getattr(receipt, "is_split_payment", False))
            raw_splits = getattr(receipt, "splits", None) or []
            for sp in raw_splits:
                sp_card = clean_card_digits(getattr(sp, "card_last_four", None))
                try:
                    sp_amt = float(getattr(sp, "amount", 0.0))
                except (ValueError, TypeError):
                    sp_amt = 0.0
                sp_method = (getattr(sp, "payment_method", "") or "").strip().lower()
                parsed_splits.append({
                    "payment_method": sp_method,
                    "amount": sp_amt,
                    "card_last_four": sp_card or "",
                })
            if len(parsed_splits) >= 2:
                is_split = True

            clean_category = (
                receipt.category.strip()
                if receipt.category and str(receipt.category).strip().lower() not in ("none", "null")
                else ""
            )
            clean_budget = (
                receipt.budget.strip()
                if receipt.budget and str(receipt.budget).strip().lower() not in ("none", "null")
                else ""
            )

            extracted_list.append({
                "date": receipt.date,
                "time": receipt.time or "",
                "amount": receipt.amount,
                "store_name": receipt.store_name,
                "description": receipt.description,
                "category": clean_category,
                "budget": clean_budget,
                "payment_method": (receipt.payment_method or "").strip().lower(),
                "card_last_four": clean_card or "",
                "is_split_payment": is_split,
                "splits": parsed_splits,
                "available_categories": categories,
                "available_budgets": budgets,
                "image_base64": "",
                "transaction_type": (getattr(receipt, "transaction_type", "withdrawal") or "withdrawal").strip().lower(),
                "destination_account": (getattr(receipt, "destination_account", "") or "").strip(),
            })

        print(f"Extracted {len(extracted_list)} transaction(s) from text.")
        return extracted_list
    except Exception as e:
        print(f"Unexpected error in extract_receipt_data_from_text: {str(e)}")
        raise



async def extract_receipt_data(
    file: UploadFile | bytes,
    filename: str | None = None,
    system_prompt: str | None = None,
    use_ocr: bool | None = None,
):
    """Extract data from the receipt image without creating a transaction."""
    try:
        fname = filename or (
            file.filename if isinstance(file, UploadFile) else "receipt.jpg"
        )
        print(f"Processing receipt file: {fname}")

        # Get raw bytes of the original uploaded file
        if isinstance(file, bytes):
            raw_bytes = file
        else:
            raw_bytes = await file.read()
            try:
                await file.seek(0)
            except Exception:
                pass

        # Detect if the file is a PDF
        is_pdf = (
            raw_bytes[:1024].find(b"%PDF-") != -1
            or (fname and fname.lower().endswith(".pdf"))
        )
        original_file_base64 = base64.b64encode(raw_bytes).decode("utf-8")

        # Process the image/PDF for visual LLM extraction and web UI preview
        image, image_bytes = await process_image(
            raw_bytes, max_size=(1280, 1600), filename=fname
        )
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        print("File processed and preview generated")

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

        # Check if local OCR is enabled
        settings = get_settings()
        effective_use_ocr = (
            use_ocr if use_ocr is not None else getattr(settings, "ocr_enabled", False)
        )
        ocr_text = None
        if effective_use_ocr:
            lang = getattr(settings, "ocr_language", "eng+deu")
            print(f"Local OCR enabled. Extracting text locally (lang: {lang})...")
            try:
                ocr_text = run_local_ocr(
                    image,
                    raw_bytes=raw_bytes,
                    is_pdf=is_pdf,
                    language=lang,
                )
                if ocr_text and len(ocr_text.strip()) >= 15:
                    print(
                        f"Local OCR successfully extracted {len(ocr_text.strip())} characters."
                    )
                else:
                    print(
                        "Local OCR returned empty or insufficient text. Falling back to vision extraction."
                    )
                    ocr_text = None
            except Exception as e:
                print(f"Warning: Local OCR failed ({e}). Falling back to vision extraction.")
                ocr_text = None

        if ocr_text:
            print("Sending OCR text to LLM (no image payload)...")
            prompt = build_extraction_prompt(
                categories, budgets, receipt_text=ocr_text, system_prompt=system_prompt
            )
            llm_res = await _run_llm_extraction(
                prompt,
                image=None,
                image_base64=None,
                settings=settings,
                response_model=MultiReceiptModel,
            )
            if isinstance(llm_res, MultiReceiptModel) and llm_res.transactions:
                receipt = llm_res.transactions[0]
            elif isinstance(llm_res, ReceiptModel):
                receipt = llm_res
            else:
                receipt = ReceiptModel(
                    date=datetime.now().strftime("%Y-%m-%d"),
                    amount=0.0,
                    store_name="Unknown Store",
                    description=ocr_text[:100].strip(),
                    category=categories[0] if categories else "Groceries",
                    budget=budgets[0] if budgets else "Monthly",
                )
        else:
            prompt = build_extraction_prompt(
                categories, budgets, system_prompt=system_prompt
            )
            receipt = await _run_llm_extraction(
                prompt, image=image, image_base64=image_base64, settings=settings
            )

        # Validate and format the date and time
        if not receipt.time:
            receipt.time = extract_time_from_date_string(receipt.date)
        receipt.date = normalize_date(receipt.date)
        receipt.time = normalize_time(receipt.time)
        clean_card = clean_card_digits(receipt.card_last_four)

        # Parse splits if split payment
        parsed_splits = []
        is_split = bool(getattr(receipt, "is_split_payment", False))
        raw_splits = getattr(receipt, "splits", None) or []
        for sp in raw_splits:
            sp_card = clean_card_digits(sp.card_last_four)
            try:
                sp_amt = float(sp.amount)
            except (ValueError, TypeError):
                sp_amt = 0.0
            parsed_splits.append({
                "payment_method": (sp.payment_method or "").strip().lower(),
                "amount": sp_amt,
                "card_last_four": sp_card or "",
            })
        if len(parsed_splits) >= 2:
            is_split = True

        clean_category = (
            receipt.category.strip()
            if receipt.category and str(receipt.category).strip().lower() not in ("none", "null")
            else ""
        )
        clean_budget = (
            receipt.budget.strip()
            if receipt.budget and str(receipt.budget).strip().lower() not in ("none", "null")
            else ""
        )

        # Return the extracted data as a dictionary
        extracted_data = {
            "date": receipt.date,
            "time": receipt.time or "",
            "amount": receipt.amount,
            "store_name": receipt.store_name,
            "description": receipt.description,
            "category": clean_category,
            "budget": clean_budget,
            "payment_method": (receipt.payment_method or "").strip().lower(),
            "card_last_four": clean_card or "",
            "is_split_payment": is_split,
            "splits": parsed_splits,
            "available_categories": categories,
            "available_budgets": budgets,
            "image_base64": image_base64,
            "original_file_base64": original_file_base64,
            "original_filename": fname,
            "is_pdf": is_pdf,
            "transaction_type": (getattr(receipt, "transaction_type", "withdrawal") or "withdrawal").strip().lower(),
            "destination_account": (getattr(receipt, "destination_account", "") or "").strip(),
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
    tx_type = (receipt_data.get("transaction_type") or "withdrawal").strip().lower()
    dest_account = (receipt_data.get("destination_account") or "").strip()
    raw_budget = receipt_data.get("budget")
    clean_budget = (
        raw_budget.strip()
        if raw_budget and str(raw_budget).strip().lower() not in ("none", "null")
        else None
    )

    raw_cat = receipt_data.get("category")
    clean_category = (
        raw_cat.strip()
        if raw_cat and str(raw_cat).strip().lower() not in ("none", "null")
        else None
    )

    # Create a ReceiptModel object from the data
    receipt = ReceiptModel(
        date=receipt_data["date"],
        time=receipt_data.get("time") or None,
        amount=receipt_data["amount"],
        store_name=receipt_data["store_name"],
        description=receipt_data["description"],
        category=clean_category,
        budget=clean_budget,
        transaction_type=tx_type,
        destination_account=dest_account or None,
    )

    # Check if this is a split payment across multiple accounts
    splits = receipt_data.get("splits")
    if splits and isinstance(splits, list) and len(splits) >= 2:
        print(f"Creating split transactions for {len(splits)} accounts...")
        created_summaries = []
        errors = []

        for idx, split in enumerate(splits):
            split_acc = split.get("account") or source_account
            try:
                split_amt = float(split.get("amount", 0.0))
            except (ValueError, TypeError):
                continue
            if split_amt <= 0:
                continue

            split_desc = f"{receipt.description} (Split {idx + 1}/{len(splits)})"
            split_receipt = ReceiptModel(
                date=receipt.date,
                time=receipt.time,
                amount=split_amt,
                store_name=receipt.store_name,
                description=split_desc,
                category=clean_category,
                budget=clean_budget,
            )

            try:
                tx_res = create_firefly_transaction(
                    split_receipt, split_acc, tags=["automated", "split-payment"]
                )
                if tx_res and "data" in tx_res:
                    tx_id = tx_res["data"]["id"]
                    created_summaries.append(f"{split_acc} ({split_amt:.2f})")
                    if image_bytes:
                        try:
                            journal_id = int(
                                tx_res["data"]["attributes"]["transactions"][0][
                                    "transaction_journal_id"
                                ]
                            )
                            attach_image_to_transaction(journal_id, image_bytes, filename)
                        except Exception as e:
                            print(f"Warning: Failed to attach image to split journal: {e}")
                else:
                    errors.append(f"Failed to create split for {split_acc}")
            except Exception as e:
                errors.append(f"Error on {split_acc}: {e}")

        if created_summaries:
            msg = f"Split payment created: {', '.join(created_summaries)}"
            if errors:
                msg += f" (Warnings: {'; '.join(errors)})"
            return msg
        else:
            return f"Failed to create split transactions: {'; '.join(errors)}"

    # Implement retry logic with exponential backoff for single transactions
    max_retries = 3  # Reduced from 5 to 3 to prevent too many duplicates
    retry_delay = 3  # Keep at 3 seconds
    last_error = None

    for attempt in range(max_retries):
        try:
            # Create a transaction based on the receipt data
            if tx_type != "withdrawal" or dest_account:
                transaction_result = create_firefly_transaction(
                    receipt,
                    source_account,
                    transaction_type=tx_type,
                    destination_account=dest_account,
                )
            else:
                transaction_result = create_firefly_transaction(receipt, source_account)

            if transaction_result:
                print("Transaction created successfully:")
                print(f"- Type: {tx_type}")
                print(f"- Date: {receipt.date}")
                print(f"- Amount: {receipt.amount}")
                if tx_type == "transfer":
                    print(f"- From: {source_account} -> To: {dest_account or receipt.store_name}")
                else:
                    print(f"- Store: {receipt.store_name}")
                    print(f"- Source Account: {source_account}")
                print(f"- Category: {receipt.category}")
                print(f"- Budget: {receipt.budget}")
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


async def refine_csv_transactions_with_llm(
    transactions: list[dict],
    system_prompt: str,
    categories: list[str] | None = None,
    budgets: list[str] | None = None,
    asset_accounts: list[str] | None = None,
) -> list[dict]:
    """Use LLM with custom system prompt to quickly refine, categorize, or correct CSV-parsed transactions."""
    if not system_prompt or not system_prompt.strip() or not transactions:
        return transactions

    clean_sp = system_prompt.strip()
    if categories is None:
        categories = get_firefly_categories() or [
            "Groceries",
            "Dining",
            "Shopping",
            "Transportation",
            "Entertainment",
            "Other",
        ]
    if budgets is None:
        budgets = get_firefly_budgets() or ["Monthly", "Weekly", "Other"]

    print(
        f"Refining {len(transactions)} CSV transaction(s) using LLM with system prompt: '{clean_sp[:80]}...'"
    )

    chunk_size = 35
    chunks = [
        transactions[i : i + chunk_size]
        for i in range(0, len(transactions), chunk_size)
    ]

    async def _process_chunk(chunk: list[dict], offset: int) -> list[dict]:
        chunk_items = [
            {
                "index": offset + idx,
                "store": tx.get("store_name", ""),
                "description": tx.get("description", ""),
                "amount": tx.get("amount", 0.0),
                "category": tx.get("category", ""),
                "budget": tx.get("budget", ""),
            }
            for idx, tx in enumerate(chunk)
        ]

        prompt = (
            "--- USER CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---\n"
            f"{clean_sp}\n"
            "Apply these instructions to review and refine the following bank/expense transactions.\n"
            "------------------------------------------------\n\n"
            "Candidate transactions:\n"
            f"{json.dumps(chunk_items, ensure_ascii=False)}\n\n"
            "Available Firefly III Categories: "
            + ", ".join(categories)
            + "\n"
            "Available Firefly III Budgets: "
            + (", ".join(budgets) if budgets else "None")
            + "\n"
            "Available Firefly III Asset Accounts: "
            + (", ".join(asset_accounts or []) if asset_accounts else "None")
            + "\n\n"
            "Instructions:\n"
            "1. Apply the user's custom instructions to update fields that need changes (category, budget, store_name, or transaction_type).\n"
            "2. If no budget applies or the user specifies no budget, set budget to null. If no category applies or the user specifies no category, set category to null.\n"
            "3. If a transaction represents an ATM cash withdrawal or account transfer, set transaction_type to 'transfer' and destination_account to the target asset account (e.g. 'Cash wallet').\n"
            "4. Return a JSON object with 'updates': a list of objects containing at minimum 'index' (int), 'category' (str or null), 'budget' (str or null), and optionally 'store_name', 'transaction_type', 'destination_account'."
        )

        try:
            # Enforce 15-second timeout on each chunk so it never hangs
            res = await asyncio.wait_for(
                _run_llm_extraction(
                    prompt,
                    image=None,
                    image_base64=None,
                    response_model=CsvRefineResponse,
                ),
                timeout=15.0,
            )

            updates_list = []
            if isinstance(res, CsvRefineResponse):
                updates_list = res.updates
            elif isinstance(res, dict) and "updates" in res:
                updates_list = res["updates"]

            updates_by_idx = {}
            for u in updates_list:
                idx_val = (
                    getattr(u, "index", None)
                    if not isinstance(u, dict)
                    else u.get("index")
                )
                if idx_val is not None:
                    updates_by_idx[int(idx_val)] = u

            refined_chunk = []
            for idx, orig_tx in enumerate(chunk):
                abs_idx = offset + idx
                merged = dict(orig_tx)
                if abs_idx in updates_by_idx:
                    up = updates_by_idx[abs_idx]
                    up_cat = (
                        getattr(up, "category", None)
                        if not isinstance(up, dict)
                        else up.get("category")
                    )
                    up_bud = (
                        getattr(up, "budget", None)
                        if not isinstance(up, dict)
                        else up.get("budget")
                    )
                    up_store = (
                        getattr(up, "store_name", None)
                        if not isinstance(up, dict)
                        else up.get("store_name")
                    )
                    up_type = (
                        getattr(up, "transaction_type", None)
                        if not isinstance(up, dict)
                        else up.get("transaction_type")
                    )
                    up_dest = (
                        getattr(up, "destination_account", None)
                        if not isinstance(up, dict)
                        else up.get("destination_account")
                    )

                    if up_cat is not None:
                        clean_c = (
                            up_cat.strip() if isinstance(up_cat, str) else ""
                        )
                        merged["category"] = (
                            ""
                            if clean_c.lower() in ("none", "null")
                            else clean_c
                        )
                    if up_bud is not None:
                        clean_b = (
                            up_bud.strip() if isinstance(up_bud, str) else ""
                        )
                        merged["budget"] = (
                            ""
                            if clean_b.lower() in ("none", "null")
                            else clean_b
                        )
                    else:
                        merged["budget"] = ""
                    if (
                        up_store
                        and up_store.strip()
                        and up_store.strip().lower() != "unknown store"
                    ):
                        merged["store_name"] = up_store.strip()
                    if up_type:
                        merged["transaction_type"] = up_type.strip().lower()
                    if up_dest:
                        merged["destination_account"] = up_dest.strip()

                refined_chunk.append(merged)
            return refined_chunk

        except Exception as e:
            print(
                f"Warning: LLM refinement of CSV chunk (offset {offset}) failed or timed out ({e}), using deterministic items."
            )
            return chunk

    # Run chunks concurrently in parallel
    tasks = [_process_chunk(chunk, i * chunk_size) for i, chunk in enumerate(chunks)]
    results = await asyncio.gather(*tasks)

    flattened = []
    for r in results:
        flattened.extend(r)
    return flattened
