import re

try:
    from pydantic import BaseModel, field_validator
    _validator_dec = field_validator("amount", mode="before")
except ImportError:
    from pydantic import BaseModel, validator
    _validator_dec = validator("amount", pre=True)


class PaymentSplit(BaseModel):
    payment_method: str | None = None
    amount: float
    card_last_four: str | None = None

    @_validator_dec
    @classmethod
    def parse_amount(cls, v):
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
            return float(cleaned)
        return v


class ReceiptModel(BaseModel):
    date: str
    time: str | None = None
    amount: float
    store_name: str
    description: str
    category: str | None = None
    budget: str | None = None
    payment_method: str | None = None
    card_last_four: str | None = None
    is_split_payment: bool = False
    splits: list[PaymentSplit] = []
    transaction_type: str = "withdrawal"
    destination_account: str | None = None

    @_validator_dec
    @classmethod
    def parse_amount(cls, v):
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
            return float(cleaned)
        return v


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

