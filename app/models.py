import re

try:
    from pydantic import BaseModel, field_validator
    _validator_dec = field_validator("amount", mode="before")
except ImportError:
    from pydantic import BaseModel, validator
    _validator_dec = validator("amount", pre=True)


class ReceiptModel(BaseModel):
    date: str
    time: str | None = None
    amount: float
    store_name: str
    description: str
    category: str
    budget: str

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
