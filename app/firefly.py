import json
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests

from .config import get_settings

# Increase timeout for all requests
TIMEOUT = 30


def get_firefly_categories():
    settings = get_settings()
    url = urljoin(settings.firefly_api_url, "categories")
    headers = {
        "Authorization": f"Bearer {settings.firefly_iii_token}",
        "Accept": "application/json",
    }
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        categories_data = response.json()["data"]
        return [category["attributes"]["name"] for category in categories_data]
    except requests.exceptions.Timeout:
        print("Request to Firefly III timed out when fetching categories")
        return []
    except requests.exceptions.RequestException as e:
        print(f"Error fetching categories: {e}")
        return []


def get_firefly_budgets():
    settings = get_settings()
    url = urljoin(settings.firefly_api_url, "budgets")
    headers = {
        "Authorization": f"Bearer {settings.firefly_iii_token}",
        "Accept": "application/json",
    }
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        budgets_data = response.json()["data"]
        return [budget["attributes"]["name"] for budget in budgets_data]
    except requests.exceptions.Timeout:
        print("Request to Firefly III timed out when fetching budgets")
        return []
    except requests.exceptions.RequestException as e:
        print(f"Error fetching budgets: {e}")
        return []


def get_firefly_asset_accounts():
    settings = get_settings()
    url = urljoin(settings.firefly_api_url, "accounts")
    headers = {
        "Authorization": f"Bearer {settings.firefly_iii_token}",
        "Accept": "application/json",
    }
    params = {
        "type": "asset"  # Only fetch asset accounts
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        accounts_data = response.json()["data"]
        return [account["attributes"]["name"] for account in accounts_data]
    except requests.exceptions.Timeout:
        print("Request to Firefly III timed out when fetching asset accounts")
        return []
    except requests.exceptions.RequestException as e:
        print(f"Error fetching asset accounts: {e}")
        return []


def check_duplicate_transactions(
    date_str: str,
    amount: float,
    store_name: str = "",
    tolerance_days: int = 1,
) -> list[dict]:
    """
    Check Firefly III for existing transactions that match the given receipt's date and amount.
    Returns a list of potential duplicate transactions.
    """
    settings = get_settings()
    if not settings.firefly_iii_url or not settings.firefly_iii_token:
        return []

    try:
        target_amount = float(amount)
        if target_amount <= 0:
            return []
    except (ValueError, TypeError):
        return []

    try:
        target_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return []

    start_date = target_date - timedelta(days=max(0, tolerance_days))
    end_date = target_date + timedelta(days=max(0, tolerance_days))

    url = urljoin(settings.firefly_api_url, "transactions")
    headers = {
        "Authorization": f"Bearer {settings.firefly_iii_token}",
        "Accept": "application/json",
    }
    params = {
        "start": start_date.strftime("%Y-%m-%d"),
        "end": end_date.strftime("%Y-%m-%d"),
        "limit": 100,
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json().get("data", [])
    except Exception as e:
        print(f"Warning: Could not check for duplicate transactions in Firefly III: {e}")
        return []

    duplicates = []
    store_clean = (store_name or "").strip().lower()

    for item in data:
        attributes = item.get("attributes", {})
        sub_txs = attributes.get("transactions", [])
        for sub in sub_txs:
            # Skip income deposits
            if sub.get("type") == "deposit":
                continue

            try:
                sub_amount = float(sub.get("amount", 0.0))
            except (ValueError, TypeError):
                continue

            # Check if amounts match within 0.01
            if abs(abs(sub_amount) - abs(target_amount)) > 0.01:
                continue

            sub_date_str = (sub.get("date") or "")[:10]
            sub_dest = (sub.get("destination_name") or "").strip().lower()
            sub_desc = (sub.get("description") or "").strip().lower()

            date_matches = (sub_date_str == date_str)
            store_matches = bool(
                store_clean
                and (
                    store_clean == sub_dest
                    or store_clean in sub_dest
                    or sub_dest in store_clean
                    or store_clean in sub_desc
                )
            )

            # Determine confidence & reason
            if date_matches and store_matches:
                confidence = "high"
                reason = "Exact match: same date, amount, and store"
            elif date_matches:
                confidence = "medium"
                reason = "Same date and amount"
            elif store_matches:
                confidence = "medium"
                try:
                    diff = (
                        datetime.strptime(sub_date_str, "%Y-%m-%d").date()
                        - target_date
                    ).days
                    day_word = (
                        "yesterday"
                        if diff == -1
                        else ("tomorrow" if diff == 1 else f"{abs(diff)} days away")
                    )
                except Exception:
                    day_word = "nearby date"
                reason = f"Same amount and store ({day_word})"
            else:
                # Different date and different store: skip to avoid false positives
                continue

            duplicates.append(
                {
                    "id": sub.get("transaction_journal_id") or item.get("id"),
                    "date": sub_date_str,
                    "amount": sub_amount,
                    "currency_symbol": sub.get("currency_symbol") or "$",
                    "currency_code": sub.get("currency_code") or "",
                    "description": sub.get("description") or "",
                    "destination_name": sub.get("destination_name") or "Unknown",
                    "source_name": sub.get("source_name") or "",
                    "category_name": sub.get("category_name") or "",
                    "budget_name": sub.get("budget_name") or "",
                    "confidence": confidence,
                    "reason": reason,
                }
            )

    return duplicates


def create_firefly_transaction(receipt, source_account="Cash wallet"):
    settings = get_settings()
    url = urljoin(settings.firefly_api_url, "transactions")
    headers = {
        "Authorization": f"Bearer {settings.firefly_iii_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Format the date to ISO 8601 format
    try:
        # Try to parse the date string into a datetime object
        date_obj = datetime.strptime(receipt.date, "%Y-%m-%d")
        # Format it as ISO 8601
        formatted_date = date_obj.strftime("%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        # If parsing fails, try to use the current date
        print(f"Invalid date format: {receipt.date}. Using current date instead.")
        formatted_date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")

    payload = {
        "transactions": [
            {
                "type": "withdrawal",
                "date": formatted_date,
                "amount": str(receipt.amount),
                "description": receipt.description,
                "destination_name": receipt.store_name,
                "source_name": source_account,
                "category_name": receipt.category,
                "budget_name": receipt.budget,
                "tags": ["automated"],
            }
        ]
    }

    try:
        print(f"Sending transaction to Firefly III: {json.dumps(payload, indent=2)}")
        response = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)

        # Log response details for debugging
        print(f"Response status: {response.status_code}")
        print(f"Response headers: {response.headers}")

        if response.status_code in [200, 201]:  # Accept both 200 and 201 as success
            return response.json()
        elif response.status_code == 401:
            raise Exception(
                "Authentication failed. Please check your Firefly III API token."
            )
        elif response.status_code == 403:
            raise Exception(
                "You don't have permission to create transactions. Please check your Firefly III permissions."
            )
        elif response.status_code == 404:
            raise Exception(
                "The Firefly III API endpoint was not found. Please check your Firefly III URL."
            )
        elif response.status_code == 422:
            error_data = response.json()
            error_message = "Validation error: "
            if "message" in error_data:
                error_message += error_data["message"]
            else:
                error_message += "Invalid data provided to Firefly III."
            raise Exception(error_message)
        elif response.status_code >= 500:
            raise Exception(
                f"Firefly III server error (HTTP {response.status_code}). The server might be experiencing issues."
            )
        else:
            error_message = f"Error creating transaction: HTTP {response.status_code}"
            try:
                error_data = response.json()
                if "message" in error_data:
                    error_message += f" - {error_data['message']}"
            except Exception:
                error_message += f" - {response.text}"
            raise Exception(error_message)
    except requests.exceptions.Timeout:
        print("Request to Firefly III timed out when creating transaction")
        raise Exception(
            "The request to Firefly III timed out. The server might be experiencing high load or connectivity issues."
        )
    except requests.exceptions.ConnectionError:
        print("Connection error when creating transaction")
        raise Exception(
            "Could not connect to Firefly III. Please check your internet connection and Firefly III URL."
        )
    except requests.exceptions.RequestException as e:
        print(f"Error creating transaction: {e}")
        raise Exception(f"Error communicating with Firefly III: {str(e)}")


def attach_image_to_transaction(
    transaction_journal_id: int, image_bytes: bytes, filename: str
):
    """Attach an image to a Firefly III transaction journal."""
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.firefly_iii_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Step 1: Create the attachment metadata
    url = urljoin(settings.firefly_api_url, "attachments")
    payload = {
        "filename": filename,
        "attachable_type": "TransactionJournal",
        "attachable_id": transaction_journal_id,
    }

    response = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    response.raise_for_status()
    attachment_id = response.json()["data"]["id"]

    # Step 2: Upload the image bytes
    upload_url = urljoin(settings.firefly_api_url, f"attachments/{attachment_id}/upload")
    upload_headers = {
        "Authorization": f"Bearer {settings.firefly_iii_token}",
        "Content-Type": "application/octet-stream",
    }

    response = requests.post(
        upload_url, headers=upload_headers, data=image_bytes, timeout=TIMEOUT
    )
    response.raise_for_status()
    print(f"Attachment {attachment_id} uploaded for journal {transaction_journal_id}")
