from datetime import datetime
import re

try:
    from pydantic import BaseModel, field_validator
    _validator_dec = field_validator("amount", mode="before")
    _validator_store = field_validator("store_name", mode="before")
    _validator_desc = field_validator("description", mode="before")
    _validator_date = field_validator("date", mode="before")
    _validator_txtype = field_validator("transaction_type", mode="before")
    _validator_splittype = field_validator("split_type", mode="before")
except ImportError:
    from pydantic import BaseModel, validator
    _validator_dec = validator("amount", pre=True, always=True)
    _validator_store = validator("store_name", pre=True, always=True)
    _validator_desc = validator("description", pre=True, always=True)
    _validator_date = validator("date", pre=True, always=True)
    _validator_txtype = validator("transaction_type", pre=True, always=True)
    _validator_splittype = validator("split_type", pre=True, always=True)


class PaymentSplit(BaseModel):
    split_type: str = "withdrawal"
    payment_method: str | None = None
    amount: float = 0.0
    card_last_four: str | None = None
    account: str | None = None
    destination_account: str | None = None
    description: str | None = None
    category: str | None = None

    @_validator_splittype
    @classmethod
    def parse_split_type(cls, v):
        if v is None or not str(v).strip():
            return "withdrawal"
        norm = str(v).strip().lower()
        if norm in ("transfer", "friend", "owed", "debt", "reimbursement"):
            return "transfer"
        return "withdrawal"

    @_validator_dec
    @classmethod
    def parse_amount(cls, v):
        if v is None or v == "":
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            cleaned = re.sub(r"[^\d.,-]", "", v.strip())
            if "," in cleaned and "." in cleaned:
                if cleaned.rfind(",") > cleaned.rfind("."):
                    cleaned = cleaned.replace(".", "").replace(",", ".")
                else:
                    cleaned = cleaned.replace(",", "")
            elif "," in cleaned:
                cleaned = cleaned.replace(",", ".")
            try:
                return float(cleaned)
            except ValueError:
                return 0.0
        return 0.0


class ReceiptModel(BaseModel):
    date: str = ""
    time: str | None = None
    amount: float = 0.0
    store_name: str = "Unknown Store"
    description: str = "Receipt"
    category: str | None = None
    budget: str | None = None
    payment_method: str | None = None
    card_last_four: str | None = None
    is_split_payment: bool = False
    splits: list[PaymentSplit] = []
    transaction_type: str = "withdrawal"
    destination_account: str | None = None

    @_validator_store
    @classmethod
    def parse_store_name(cls, v):
        if v is None or not str(v).strip() or str(v).strip().lower() in ("none", "null"):
            return "Unknown Store"
        return str(v).strip()

    @_validator_desc
    @classmethod
    def parse_description(cls, v):
        if v is None or not str(v).strip() or str(v).strip().lower() in ("none", "null"):
            return "Receipt"
        return str(v).strip()

    @_validator_date
    @classmethod
    def parse_date(cls, v):
        if v is None or not str(v).strip() or str(v).strip().lower() in ("none", "null"):
            return datetime.now().strftime("%Y-%m-%d")
        return str(v).strip()

    @_validator_txtype
    @classmethod
    def parse_transaction_type(cls, v):
        if v is None or not str(v).strip() or str(v).strip().lower() not in ("withdrawal", "transfer", "deposit"):
            return "withdrawal"
        return str(v).strip().lower()

    @_validator_dec
    @classmethod
    def parse_amount(cls, v):
        if v is None or v == "":
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            cleaned = re.sub(r"[^\d.,-]", "", v.strip())
            if "," in cleaned and "." in cleaned:
                if cleaned.rfind(",") > cleaned.rfind("."):
                    cleaned = cleaned.replace(".", "").replace(",", ".")
                else:
                    cleaned = cleaned.replace(",", "")
            elif "," in cleaned:
                cleaned = cleaned.replace(",", ".")
            try:
                return float(cleaned)
            except ValueError:
                return 0.0
        return 0.0


class MultiReceiptModel(BaseModel):
    transactions: list[ReceiptModel] = []


class CsvTransactionUpdate(BaseModel):
    index: int
    category: str | None = None
    budget: str | None = None
    store_name: str | None = None
    transaction_type: str | None = None
    destination_account: str | None = None


class CsvRefineResponse(BaseModel):
    updates: list[CsvTransactionUpdate] = []


def validate_model_json(model_cls, json_str: str):
    """Validate JSON string into model instance supporting both Pydantic v1 and v2."""
    if hasattr(model_cls, "model_validate_json"):
        return model_cls.model_validate_json(json_str)
    return model_cls.parse_raw(json_str)


def validate_model_obj(model_cls, obj: dict):
    """Validate dictionary into model instance supporting both Pydantic v1 and v2."""
    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(obj)
    return model_cls.parse_obj(obj)

