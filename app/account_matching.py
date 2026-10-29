import json
import os
import re
from typing import Any

from .config import get_settings


def clean_card_digits(val: Any) -> str | None:
    """Extract and return the last 4 digits of a card number or string."""
    if not val or not str(val).strip():
        return None
    raw = str(val).strip()
    if raw.lower() in ("null", "none", "unknown", "n/a", "cash"):
        return None
    cleaned = re.sub(r"\D", "", raw)
    if len(cleaned) >= 4:
        return cleaned[-4:]
    return None


def _normalize_mapping_dict(data: dict) -> dict[str, str]:
    """Helper to normalize mappings where keys can be card digits or accounts."""
    result = {}
    for k, v in data.items():
        if isinstance(v, (list, tuple, set)):
            # Format: {"Easybank Giro": ["1111", "2222"]}
            account_name = str(k).strip()
            for card in v:
                card_str = str(card).strip().lower()
                if card_str:
                    result[card_str] = account_name
        else:
            k_str = str(k).strip()
            v_str = str(v).strip()
            # If key contains multiple cards e.g. "1111, 2222" or "1111/2222" or "1111|2222"
            if any(sep in k_str for sep in ["/", "|", ","]):
                cards = [c.strip().lower() for c in re.split(r"[/|,\s]+", k_str) if c.strip()]
                for card in cards:
                    result[card] = v_str
            # If value is card digits/cash and key is account name e.g. {"Easybank Giro": "1111"}
            elif (clean_card_digits(v_str) is not None or v_str.lower() in ("cash", "bar", "bargeld")) and clean_card_digits(k_str) is None:
                result[v_str.lower()] = k_str
            else:
                result[k_str.lower()] = v_str
    return result


def parse_account_card_mappings(raw: str | dict | None) -> dict[str, str]:
    """
    Parse account card mappings from either:
    1. A dictionary (supports {"1234": "Account"} or {"Account": ["1234", "5678"]})
    2. A JSON string
    3. Delimited pairs e.g.:
       - '1234:Checking, 5678:Checking, cash:Cash wallet'
       - '1234/5678:Checking, cash:Cash wallet'
       - '1234|5678:Checking, cash:Cash wallet'
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return _normalize_mapping_dict(raw)

    s = str(raw).strip()
    if not s:
        return {}

    # 1. Try parsing as JSON
    if s.startswith("{") and s.endswith("}"):
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                return _normalize_mapping_dict(data)
        except Exception as e:
            print(f"Warning: Failed to parse ACCOUNT_CARD_MAPPINGS as JSON: {e}")

    # 2. Parse delimited key:value or key=value pairs
    result = {}
    entries = re.split(r"[,;\n]+", s)
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            key, _, val = entry.partition(":")
        elif "=" in entry:
            key, _, val = entry.partition("=")
        else:
            continue

        target_acc = val.strip()
        # Key can have multiple cards separated by / or | e.g. "1234/5678"
        cards = [c.strip().lower() for c in re.split(r"[/|\s]+", key.strip()) if c.strip()]
        for card in cards:
            if card and target_acc:
                result[card] = target_acc

    return result


def load_account_mappings() -> dict[str, str]:
    """
    Load account card mappings from account_cards.json or environment variables.
    Environment variable (ACCOUNT_CARD_MAPPINGS) takes precedence.
    """
    mappings: dict[str, str] = {}

    # Check for optional account_cards.json file in root or config/
    for file_path in ["account_cards.json", "config/account_cards.json"]:
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        mappings.update(
                            {
                                str(k).strip().lower(): str(v).strip()
                                for k, v in data.items()
                            }
                        )
                break
            except Exception as e:
                print(f"Warning: Failed to load mappings from {file_path}: {e}")

    # Load from Settings / environment variable
    settings = get_settings()
    env_raw = getattr(settings, "account_card_mappings", "")
    if env_raw:
        env_mappings = parse_account_card_mappings(env_raw)
        mappings.update(env_mappings)

    return mappings


def match_account_for_receipt(
    payment_method: str | None,
    card_last_four: str | None,
    asset_accounts: list[str],
) -> tuple[str | None, str | None]:
    """
    Match an asset account based on card_last_four or cash payment.

    Args:
        payment_method: 'card', 'cash', or None/other
        card_last_four: Digits string like '1234' or None
        asset_accounts: Available Firefly III asset account names

    Returns:
        (matched_account_name, reason) or (None, None)
    """
    if not asset_accounts:
        return None, None

    mappings = load_account_mappings()
    clean_card = clean_card_digits(card_last_four)
    norm_method = (payment_method or "").strip().lower()

    # Helper function for case-insensitive account resolution
    def find_account(target_name: str) -> str | None:
        target_lower = target_name.strip().lower()
        # 1. Exact match (case-insensitive)
        for acc in asset_accounts:
            if acc.lower() == target_lower:
                return acc
        # 2. Substring match
        for acc in asset_accounts:
            if target_lower in acc.lower() or acc.lower() in target_lower:
                return acc
        return None

    # 1. Check card last 4 digits
    if clean_card:
        # Check explicit mapping for the 4 digits
        if clean_card in mappings:
            matched = find_account(mappings[clean_card])
            if matched:
                return matched, f"Card ending in {clean_card} detected"

        # Check if an asset account name itself ends in or contains the 4 digits (e.g. 'Visa (*1234)')
        for acc in asset_accounts:
            if clean_card in acc:
                return acc, f"Account matched card ending in {clean_card}"

    # 2. Check cash payment
    if norm_method == "cash" or (not clean_card and "bar" in norm_method):
        # Check explicit mapping for 'cash' or 'bar'
        for cash_key in ["cash", "bar", "bargeld"]:
            if cash_key in mappings:
                matched = find_account(mappings[cash_key])
                if matched:
                    return matched, "Cash payment detected"

        # Fallback: look for an asset account with 'cash' or 'bargeld' in its name
        for acc in asset_accounts:
            acc_lower = acc.lower()
            if any(w in acc_lower for w in ["cash", "bargeld", "barge"]):
                return acc, "Cash payment detected"

    return None, None


def enrich_extracted_data_accounts(
    extracted_data: dict,
    asset_accounts: list[str],
    fallback_account: str | None = None,
) -> dict:
    """
    Enrich extracted receipt data with account matching for the main account
    and any detected payment splits.
    """
    fallback = (
        fallback_account.strip()
        if fallback_account and fallback_account.strip()
        else (asset_accounts[0] if asset_accounts else "Cash wallet")
    )
    matched_acc, match_reason = match_account_for_receipt(
        extracted_data.get("payment_method"),
        extracted_data.get("card_last_four"),
        asset_accounts,
    )
    chosen_account = matched_acc or fallback
    extracted_data["source_account"] = chosen_account
    extracted_data["recommended_account"] = matched_acc
    extracted_data["account_match_reason"] = match_reason
    extracted_data["detected_card"] = extracted_data.get("card_last_four")
    extracted_data["detected_payment_method"] = extracted_data.get("payment_method")

    # Enrich split accounts if splits exist
    splits = extracted_data.get("splits") or []
    enriched_splits = []
    if isinstance(splits, list):
        for sp in splits:
            if isinstance(sp, dict):
                sp_method = sp.get("payment_method")
                sp_card = sp.get("card_last_four")
                sp_acc, sp_reason = match_account_for_receipt(
                    sp_method,
                    sp_card,
                    asset_accounts,
                )
                try:
                    sp_amt = float(sp.get("amount", 0.0) or 0.0)
                except (ValueError, TypeError):
                    sp_amt = 0.0

                enriched_splits.append({
                    "account": sp_acc or chosen_account,
                    "amount": sp_amt,
                    "payment_method": sp_method or "",
                    "card_last_four": sp_card or "",
                    "account_match_reason": sp_reason,
                })
    extracted_data["splits"] = enriched_splits
    return extracted_data


