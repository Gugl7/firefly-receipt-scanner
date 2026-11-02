import csv
import io
import re
from datetime import datetime

from .account_matching import clean_card_digits, enrich_extracted_data_accounts
from .firefly import get_firefly_budgets, get_firefly_categories


def decode_csv_bytes(content: bytes) -> str:
    """Decode raw bytes into a string trying common encodings (UTF-8, UTF-8-sig, Latin-1, CP1252)."""
    encodings = ["utf-8-sig", "utf-8", "latin-1", "cp1252", "iso-8859-15"]
    for enc in encodings:
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace")


def detect_delimiter(sample_text: str) -> str:
    """Detect CSV delimiter (semicolon, comma, tab, or pipe). Defaults to ';' if European bank format."""
    # Count occurrences across first 10 lines
    first_lines = [line for line in sample_text.splitlines()[:10] if line.strip()]
    if not first_lines:
        return ","

    text_chunk = "\n".join(first_lines)

    # Try csv.Sniffer first
    try:
        dialect = csv.Sniffer().sniff(text_chunk, delimiters=";,|\t,")
        if dialect and dialect.delimiter:
            return dialect.delimiter
    except Exception:
        pass

    # Heuristic counts
    semis = sum(line.count(";") for line in first_lines)
    commas = sum(line.count(",") for line in first_lines)
    tabs = sum(line.count("\t") for line in first_lines)
    pipes = sum(line.count("|") for line in first_lines)

    counts = [(";", semis), (",", commas), ("\t", tabs), ("|", pipes)]
    counts.sort(key=lambda x: x[1], reverse=True)

    if counts[0][1] > 0:
        return counts[0][0]
    return ","


def parse_csv_amount(val: str) -> float | None:
    """
    Parse a numeric amount string supporting both European (1.234,56 / -45,90 / 45,90-)
    and US/UK (1,234.56 / -45.90 / (45.90)) formats.
    Returns the absolute amount as a positive float for Firefly III withdrawals.
    """
    if not val or not str(val).strip():
        return None
    s = str(val).strip()

    # Check for negative indicators
    if s.endswith("-"):
        s = s[:-1].strip()
    elif s.startswith("-"):
        s = s[1:].strip()
    elif s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()

    # Strip currency symbols and letters (EUR, €, $, USD, etc.)
    s = re.sub(r"[^\d.,]", "", s).strip()
    if not s:
        return None

    # Determine decimal separator
    if "." in s and "," in s:
        if s.rfind(",") > s.rfind("."):
            # European format: 1.234,56 -> 1234.56
            s = s.replace(".", "").replace(",", ".")
        else:
            # US/UK format: 1,234.56 -> 1234.56
            s = s.replace(",", "")
    elif "," in s:
        # e.g. 45,90 -> 45.90
        s = s.replace(",", ".")

    try:
        amt = float(s)
        return round(abs(amt), 2)
    except ValueError:
        return None


def parse_csv_date_and_time(raw_date: str, raw_time: str | None = None) -> tuple[str, str]:
    """
    Parse date and optional time into (YYYY-MM-DD, HH:MM).
    Supports European DD.MM.YYYY, ISO YYYY-MM-DD, DD/MM/YYYY, etc.
    """
    date_clean = (raw_date or "").strip()
    time_clean = (raw_time or "").strip()

    # If time is in the date field (e.g. "28.10.2026 14:35:00" or "2026-10-28T14:35")
    if "T" in date_clean:
        parts = date_clean.split("T", 1)
        date_clean = parts[0].strip()
        if not time_clean and len(parts) > 1:
            time_clean = parts[1].strip()
    elif " " in date_clean and any(sep in date_clean.split(" ", 1)[0] for sep in (".", "-", "/")):
        parts = date_clean.split(" ", 1)
        date_clean = parts[0].strip()
        if not time_clean and len(parts) > 1:
            time_clean = parts[1].strip()

    # Parse date
    parsed_date = None
    date_formats = [
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%d.%m.%y",
        "%d/%m/%y",
        "%y-%m-%d",
    ]
    for fmt in date_formats:
        try:
            parsed_date = datetime.strptime(date_clean, fmt).strftime("%Y-%m-%d")
            break
        except ValueError:
            continue

    if not parsed_date:
        parsed_date = datetime.now().strftime("%Y-%m-%d")

    # Parse time
    parsed_time = ""
    if time_clean:
        # Normalize European separators e.g. 14h35 or 14.35
        t_cleaned = time_clean.replace("h", ":").replace("H", ":").strip()
        time_formats = [
            "%H:%M:%S",
            "%H:%M",
            "%I:%M:%S %p",
            "%I:%M %p",
            "%I:%M%p",
            "%I:%M:%S%p",
        ]
        for tf in time_formats:
            try:
                parsed_time = datetime.strptime(t_cleaned, tf).strftime("%H:%M")
                break
            except ValueError:
                continue

        # Regex fallback for HH:MM
        if not parsed_time:
            m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", t_cleaned)
            if m:
                parsed_time = f"{int(m.group(1)):02d}:{m.group(2)}"

    return parsed_date, parsed_time


def detect_card_and_payment_method(text: str) -> tuple[str | None, str | None]:
    """Detect card last 4 digits and payment method ('card' or 'cash') from text."""
    if not text:
        return None, None

    clean_text = text.strip()

    # 1. Look for explicit card patterns: ...1234, ****1234, Karte 1234, PAN ...1234, Kartennr ...1234
    card_patterns = [
        r"(?:kartennr|karte|pan|card)\.?:?\s*[*xX\.\s]*(\d{4})\b",
        r"(?:\*{3,}|\.{3,}|x{3,})\s*(\d{4})\b",
        r"\b(?:ending\s+in|mit\s+endziffer)\s*(\d{4})\b",
    ]
    for pattern in card_patterns:
        m = re.search(pattern, clean_text, re.IGNORECASE)
        if m:
            digits = clean_card_digits(m.group(1))
            if digits:
                return digits, "card"

    # 2. Check for cash keywords
    cash_keywords = ["barzahlung", "bargeld", "barabhebung", "geldausgabe", "cash", "wechselgeld"]
    text_lower = clean_text.lower()
    if any(k in text_lower for k in cash_keywords):
        return None, "cash"

    # 3. Check for general card payment keywords
    card_keywords = ["kartenzahlung", "pos-zahlung", "debitkarte", "kreditkarte", "visa", "mastercard", "maestro"]
    if any(k in text_lower for k in card_keywords):
        return None, "card"

    return None, None


def clean_store_and_description(raw_store: str, raw_desc: str) -> tuple[str, str]:
    """Clean store name and description, deriving store from description if empty."""
    store = (raw_store or "").strip()
    desc = (raw_desc or "").strip()

    # Common prefixes to strip when deriving store name
    prefixes = [
        r"^kartenzahlung(?:\s+vom\s+[\d.:\s]+)?\s*",
        r"^pos-zahlung(?:\s+vom\s+[\d.:\s]+)?\s*",
        r"^zahlung\s+an\s*",
        r"^überweisung\s+an\s*",
        r"^sepa-lastschrift(?:\s+von)?\s*",
        r"^lastschrift(?:\s+von)?\s*",
        r"^bezahlung\s+bei\s*",
        r"^debit\s+card\s+payment\s+to\s*",
        r"^purchase\s+at\s*",
    ]

    if not store and desc:
        candidate = desc
        for p in prefixes:
            candidate = re.sub(p, "", candidate, flags=re.IGNORECASE).strip()
        # Take first part before extra details or dates
        candidate = re.split(r"(?:\s+\d{2}\.\d{2}\.|\s+ref:|\s+iban|\s+bic)", candidate, flags=re.IGNORECASE)[0].strip()
        store = candidate or desc
    elif store and not desc:
        desc = store

    if not store and not desc:
        store = "Transaction"
        desc = "Transaction"

    return store, desc


def guess_category(store: str, desc: str, available_categories: list[str]) -> str:
    """Guess best matching Firefly III category based on merchant and description keywords."""
    if not available_categories:
        return "Other"

    # Map of category types to merchant keywords
    keyword_groups = {
        "groceries": [
            "billa", "spar", "hofer", "lidl", "penny", "mpreis", "merkur",
            "supermarkt", "grocery", "groceries", "aldi", "edeka", "rewe",
            "lebensmittel", "trader joe", "whole foods", "kaufland", "denner",
            "coop", "migros", "supermarket", "nah&frisch"
        ],
        "dining": [
            "restaurant", "cafe", "café", "bäckerei", "bakery", "mcdonald",
            "burger", "pizza", "subway", "bar", "bistro", "starbucks",
            "food", "gastronomie", "essen", "do & co", "nordsee", "vapiano",
            "sushi", "kebap", "kebab", "döner", "konditorei", "anpassung"
        ],
        "transportation": [
            "tankstelle", "omv", "shell", "bp", "eni", "avanti", "jet",
            "uber", "taxi", "öbb", "oebb", "wiener linien", "train",
            "s-bahn", "parking", "garage", "apcoa", "parken", "tanken",
            "transport", "auto", "benzin", "diesel", "wipark", "westbahn"
        ],
        "shopping": [
            "amazon", "mediamarkt", "saturn", "ikea", "h&m", "zara",
            "decathlon", "shopping", "einkaufen", "kleidung", "action",
            "thalia", "hartlauer", "xxxlutz", "kika", "moebelix", "primark"
        ],
        "health": [
            "apotheke", "pharmacy", "arzt", "doctor", "drogerie", "dm drogerie",
            "dm-drogerie", "bipa", "müller", "gesundheit", "health", "optik",
            "fielmann", "pearle"
        ],
        "entertainment": [
            "netflix", "spotify", "cinema", "kino", "steam", "nintendo",
            "playstation", "apple.com/bill", "google", "disney", "prime video",
            "entertainment", "freizeit", "cineplexx"
        ],
        "utilities": [
            "strom", "gas", "wien energie", "evn", "miete", "rent",
            "kabel", "internet", "a1", "magenta", "drei", "utilities", "wohnen"
        ],
    }

    combined = f"{store} {desc}".lower()

    # Check which category group matches
    for group_key, keywords in keyword_groups.items():
        if any(kw in combined for kw in keywords):
            # Find matching category in available_categories
            for cat in available_categories:
                cat_lower = cat.lower()
                if group_key in cat_lower or any(kw in cat_lower for kw in keywords[:3]):
                    return cat

    # Default fallback: check if any available category name is mentioned in text
    for cat in available_categories:
        if cat.lower() in combined:
            return cat

    # Return first category or 'Other'
    for cat in available_categories:
        if cat.lower() == "other" or cat.lower() == "sonstiges":
            return cat

    return available_categories[0]


def parse_csv_transactions(
    csv_content: str | bytes,
    default_account: str = "",
    asset_accounts: list[str] | None = None,
    categories: list[str] | None = None,
    budgets: list[str] | None = None,
) -> list[dict]:
    """
    Parse a CSV file (string or bytes) into a list of structured transaction dictionaries
    compatible with review.html and create_transaction_from_data.
    """
    if isinstance(csv_content, bytes):
        text = decode_csv_bytes(csv_content)
    else:
        text = str(csv_content)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    delimiter = detect_delimiter(text)
    asset_accounts = asset_accounts or ["Cash wallet"]
    if categories is None:
        try:
            categories = get_firefly_categories()
        except Exception:
            categories = None
    categories = categories or [
        "Groceries", "Dining", "Shopping", "Transportation", "Entertainment", "Other"
    ]

    if budgets is None:
        try:
            budgets = get_firefly_budgets()
        except Exception:
            budgets = None
    budgets = budgets or ["Monthly", "Weekly", "Other"]

    # 1. Identify header row (search first 15 lines)
    header_idx = 0
    date_keywords = [
        "datum", "date", "buchung", "valuta", "zeitpunkt", "tag", "transaktion",
        "time", "day", "txn", "posted", "booking"
    ]
    amount_keywords = [
        "betrag", "amount", "umsatz", "wert", "summe", "total", "soll", "haben", "debit", "credit",
        "price", "preis", "kosten", "cost", "charge", "paid", "inflow", "outflow", "value", "balance"
    ]
    payee_keywords = [
        "partner", "empfänger", "empfaenger", "begünstigter", "beguenstigter", "store", "merchant", "payee",
        "vendor", "beschreibung", "description", "text", "memo", "details", "name", "recipient", "haendler", "händler"
    ]

    for i in range(min(15, len(lines))):
        line = lines[i]
        try:
            reader = csv.reader([line], delimiter=delimiter)
            row = next(reader)
        except Exception:
            continue

        row_lowers = [col.strip().lower() for col in row]
        has_date = any(any(k in col for k in date_keywords) for col in row_lowers)
        has_amount = any(any(k in col for k in amount_keywords) for col in row_lowers)
        has_payee = any(any(k in col for k in payee_keywords) for col in row_lowers)

        if has_date and (has_amount or has_payee):
            header_idx = i
            break

    # Parse CSV with the detected header
    csv_file = io.StringIO("\n".join(lines[header_idx:]))
    reader = csv.reader(csv_file, delimiter=delimiter)

    try:
        header_row = next(reader)
    except StopIteration:
        return []

    # Map column headers to column indices
    col_map: dict[str, int] = {}
    debit_col: int | None = None
    credit_col: int | None = None

    for idx, col in enumerate(header_row):
        col_clean = re.sub(r"[^\w\s]", "", col).strip().lower()

        # Date column
        if "date" not in col_map:
            if any(k in col_clean for k in ["buchungsdatum", "transaktionsdatum", "booking date", "transaction date", "posting date", "buchungstag"]):
                col_map["date"] = idx
            elif any(k in col_clean for k in ["datum", "date", "valutadatum", "value date", "valuta", "wertstellung", "tag", "txn date", "post date", "day"]):
                col_map["date"] = idx

        # Time column
        if "time" not in col_map and any(k in col_clean for k in ["uhrzeit", "zeit", "time", "transaktionszeit"]):
            col_map["time"] = idx

        # Store / Payee column
        if "store" not in col_map:
            if any(k in col_clean for k in [
                "partnername", "beguenstigter", "begünstigter", "zahlungsempfaenger", "zahlungsempfänger",
                "empfaenger", "empfänger", "partner", "store", "merchant", "payee", "haendler", "händler",
                "kreditor", "destination"
            ]):
                col_map["store"] = idx

        # Description column
        if "description" not in col_map:
            if any(k in col_clean for k in [
                "buchungstext", "verwendungszweck", "zahlungsreferenz", "description", "beschreibung",
                "notiz", "memo", "details", "text", "kommentar", "kundenreferenz", "vermerk"
            ]):
                col_map["description"] = idx

        # Category column
        if "category" not in col_map and any(k in col_clean for k in ["kategorie", "category", "rubrik", "sparte"]):
            col_map["category"] = idx

        # Budget column
        if "budget" not in col_map and any(k in col_clean for k in ["budget", "etat"]):
            col_map["budget"] = idx

        # Card column
        if "card" not in col_map and any(k in col_clean for k in ["karte", "card", "kartennummer", "pan"]):
            col_map["card"] = idx

        # Account column
        if "account" not in col_map and any(k in col_clean for k in ["konto", "account", "iban", "kontonummer"]):
            col_map["account"] = idx

        # Amount column(s)
        if any(k in col_clean for k in ["debit", "soll", "ausgabe", "ausgaben", "lastschrift", "outflow", "withdrawal"]):
            debit_col = idx
        elif any(k in col_clean for k in ["credit", "haben", "einnahme", "einnahmen", "gutschrift", "inflow", "deposit"]):
            credit_col = idx
        elif "amount" not in col_map and "wertstellung" not in col_clean:
            if any(k in col_clean for k in ["betrag", "amount", "umsatz", "total", "summe", "sum", "preis", "price", "kosten", "cost", "charge", "paid", "value", "zahlungsbetrag"]):
                col_map["amount"] = idx
            elif col_clean == "wert" or col_clean.startswith("wert ") or col_clean.endswith(" wert"):
                col_map["amount"] = idx

    # Fallback auto-detection of date and amount columns by cell inspection if headers didn't match
    if "date" not in col_map or ("amount" not in col_map and debit_col is None and credit_col is None):
        sample_rows = [r for r in lines[header_idx + 1: header_idx + 10]]
        for r_str in sample_rows:
            try:
                r_cells = next(csv.reader([r_str], delimiter=delimiter))
            except Exception:
                continue
            for c_idx, cell in enumerate(r_cells):
                cell_s = cell.strip()
                if "date" not in col_map and re.search(r"\b(\d{1,4}[./-]\d{1,2}[./-]\d{1,4})\b", cell_s):
                    col_map["date"] = c_idx
                if "amount" not in col_map and debit_col is None and credit_col is None:
                    if parse_csv_amount(cell_s) is not None and not re.search(r"\b(\d{1,4}[./-]\d{1,2}[./-]\d{1,4})\b", cell_s):
                        col_map["amount"] = c_idx

    transactions = []

    for row in reader:
        if not row or not any(cell.strip() for cell in row):
            continue

        def get_val(key: str) -> str:
            idx = col_map.get(key)
            if idx is not None and idx < len(row):
                return row[idx].strip()
            return ""

        raw_date = get_val("date")
        raw_time = get_val("time")
        raw_store = get_val("store")
        raw_desc = get_val("description")
        raw_cat = get_val("category")
        raw_budget = get_val("budget")
        raw_card = get_val("card")
        raw_acc = get_val("account")

        # Determine Amount
        amount = None
        if debit_col is not None and debit_col < len(row):
            debit_val = parse_csv_amount(row[debit_col])
            if debit_val and debit_val > 0:
                amount = debit_val

        if amount is None and credit_col is not None and credit_col < len(row):
            credit_val = parse_csv_amount(row[credit_col])
            if credit_val and credit_val > 0:
                amount = credit_val

        if amount is None:
            raw_amt = get_val("amount")
            amount = parse_csv_amount(raw_amt)

        # Skip rows without a valid positive amount
        if amount is None or amount <= 0:
            continue

        # Date & Time parsing
        tx_date, tx_time = parse_csv_date_and_time(raw_date, raw_time)

        # Store & Description
        tx_store, tx_desc = clean_store_and_description(raw_store, raw_desc)

        # If time is still empty, look for a timestamp in the description (common in bank card transactions)
        if not tx_time and tx_desc:
            m_time = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)(?::[0-5]\d)?\b", tx_desc)
            if m_time:
                tx_time = f"{int(m_time.group(1)):02d}:{m_time.group(2)}"

        # Card & Payment Method
        detected_card, detected_method = detect_card_and_payment_method(f"{tx_store} {tx_desc} {raw_card}")
        if raw_card:
            explicit_digits = clean_card_digits(raw_card)
            if explicit_digits:
                detected_card = explicit_digits
                detected_method = "card"

        # Category
        chosen_cat = ""
        if raw_cat:
            # Check for match in available categories
            for c in categories:
                if c.lower() == raw_cat.strip().lower():
                    chosen_cat = c
                    break
            if not chosen_cat and raw_cat.strip().lower() not in ("none", "null"):
                chosen_cat = raw_cat.strip()
        elif "category" not in col_map:
            chosen_cat = guess_category(tx_store, tx_desc, categories)

        # Budget
        chosen_budget = ""
        if raw_budget:
            for b in budgets:
                if b.lower() == raw_budget.strip().lower():
                    chosen_budget = b
                    break
            if not chosen_budget and raw_budget.strip().lower() not in ("none", "null"):
                chosen_budget = raw_budget.strip()

        item_data = {
            "date": tx_date,
            "time": tx_time,
            "amount": amount,
            "store_name": tx_store,
            "description": tx_desc,
            "category": chosen_cat,
            "budget": chosen_budget,
            "payment_method": detected_method or "card",
            "card_last_four": detected_card or "",
            "is_split_payment": False,
            "splits": [],
            "available_categories": categories,
            "available_budgets": budgets,
            "image_base64": "",
        }

        # Resolve account matching
        fallback_acc = raw_acc or default_account or (asset_accounts[0] if asset_accounts else "Cash wallet")
        item_data = enrich_extracted_data_accounts(item_data, asset_accounts, fallback_acc)

        transactions.append(item_data)

    return transactions
