import asyncio
import json
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
from app.config import Settings
from app.models import ReceiptModel
from app.receipt_processing import (
    _extract_with_router,
    extract_json_str,
    normalize_date,
    parse_receipt_json,
)


def test_settings():
    print("Testing Settings...")

    # 1. Defaults with nothing set
    s = Settings(
        firefly_iii_url="http://firefly.local",
        firefly_iii_token="token123",
        google_ai_api_key="",
    )
    assert s.effective_llm_provider == "gemini", f"Expected gemini, got {s.effective_llm_provider}"
    assert s.effective_router_model == "gemini-2.5-flash"

    # 2. ROUTER_BASE_URL sets provider to 9router automatically
    s = Settings(
        firefly_iii_url="http://firefly.local",
        firefly_iii_token="token123",
        ROUTER_BASE_URL="http://localhost:20128",
    )
    assert s.effective_llm_provider == "9router", f"Expected 9router, got {s.effective_llm_provider}"
    assert s.router_chat_url == "http://localhost:20128/v1/chat/completions"

    # 3. URL normalization variants
    cases = [
        ("http://localhost:20128", "http://localhost:20128/v1/chat/completions"),
        ("http://localhost:20128/", "http://localhost:20128/v1/chat/completions"),
        ("http://localhost:20128/v1", "http://localhost:20128/v1/chat/completions"),
        ("http://localhost:20128/v1/", "http://localhost:20128/v1/chat/completions"),
        ("http://localhost:20128/v1/chat/completions", "http://localhost:20128/v1/chat/completions"),
        ("http://host.docker.internal:20128/v1", "http://host.docker.internal:20128/v1/chat/completions"),
    ]
    for input_url, expected in cases:
        s = Settings(
            firefly_iii_url="http://firefly.local",
            firefly_iii_token="token123",
            ROUTER_BASE_URL=input_url,
        )
        assert s.router_chat_url == expected, f"For {input_url}: expected {expected}, got {s.router_chat_url}"

    # 4. Aliases: LLM_BASE_URL and OPENAI_BASE_URL
    s = Settings(
        firefly_iii_url="http://firefly.local",
        firefly_iii_token="token123",
        LLM_BASE_URL="http://192.168.1.100:20128/v1",
        LLM_API_KEY="my-key",
        LLM_MODEL="gpt-4o",
    )
    assert s.effective_llm_provider == "9router"
    assert s.router_chat_url == "http://192.168.1.100:20128/v1/chat/completions"
    assert s.router_api_key == "my-key"
    assert s.effective_router_model == "gpt-4o"

    # 5. Explicit provider override
    s = Settings(
        firefly_iii_url="http://firefly.local",
        firefly_iii_token="token123",
        LLM_PROVIDER="gemini",
        ROUTER_BASE_URL="http://localhost:20128/v1",
    )
    assert s.effective_llm_provider == "gemini"

    print("Settings tests passed!")


def test_models():
    print("Testing ReceiptModel...")
    r1 = ReceiptModel(
        date="2026-10-26",
        amount=19.99,
        store_name="Market",
        description="Food",
        category="Groceries",
        budget="Monthly",
    )
    assert r1.amount == 19.99

    r2 = ReceiptModel(
        date="2026-10-26",
        amount="$19.99",
        store_name="Market",
        description="Food",
        category="Groceries",
        budget="Monthly",
    )
    assert r2.amount == 19.99

    r3 = ReceiptModel(
        date="2026-10-26",
        amount="19,99 €",
        store_name="Market",
        description="Food",
        category="Groceries",
        budget="Monthly",
    )
    assert r3.amount == 19.99

    r4 = ReceiptModel(
        date="2026-10-26",
        amount="1,250.75",
        store_name="Market",
        description="Food",
        category="Groceries",
        budget="Monthly",
    )
    assert r4.amount == 1250.75

    print("ReceiptModel tests passed!")


def test_date_and_json_parsing():
    print("Testing date & JSON parsing...")

    # Date normalization
    assert normalize_date("2026-10-26") == "2026-10-26"
    assert normalize_date("26/10/2026") == "2026-10-26"
    assert normalize_date("10/26/2026") == "2026-10-26"
    assert normalize_date("26.10.2026") == "2026-10-26"

    today_str = datetime.now().strftime("%Y-%m-%d")
    assert normalize_date("not-a-date") == today_str

    # JSON extraction
    raw_json = '{"date": "2026-10-26", "amount": 10.0, "store_name": "Store", "description": "Desc", "category": "Cat", "budget": "Bud"}'
    assert parse_receipt_json(raw_json).store_name == "Store"

    markdown_json = f"```json\n{raw_json}\n```"
    assert parse_receipt_json(markdown_json).store_name == "Store"

    surrounded_json = f"Here is the result:\n{raw_json}\nHope this helps!"
    assert parse_receipt_json(surrounded_json).store_name == "Store"

    print("Date & JSON parsing tests passed!")


async def test_extract_with_router():
    print("Testing _extract_with_router with mocked httpx...")

    settings = Settings(
        firefly_iii_url="http://firefly.local",
        firefly_iii_token="token123",
        ROUTER_BASE_URL="http://localhost:20128/v1",
        ROUTER_API_KEY="test-api-key",
        ROUTER_MODEL="gemini-2.5-flash",
    )

    mock_resp_data = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "date": "2026-10-26",
                            "amount": 42.50,
                            "store_name": "Supermarket",
                            "description": "Weekly Groceries",
                            "category": "Groceries",
                            "budget": "Monthly",
                        }
                    )
                }
            }
        ]
    }

    # Test successful call
    async def mock_handler(request: httpx.Request):
        assert request.url == "http://localhost:20128/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-api-key"
        body = json.loads(request.read())
        assert body["model"] == "gemini-2.5-flash"
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][0]["content"][1]["type"] == "image_url"
        return httpx.Response(200, json=mock_resp_data)

    transport = httpx.MockTransport(mock_handler)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = await _extract_with_router("Test prompt", "base64image", settings)
        assert result.amount == 42.50
        assert result.store_name == "Supermarket"

    # Test 400 response_format fallback
    call_count = 0

    async def mock_fallback_handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        body = json.loads(request.read())
        if call_count == 1:
            assert "response_format" in body
            return httpx.Response(
                400, text='{"error": "response_format is not supported by this model"}'
            )
        else:
            assert "response_format" not in body
            return httpx.Response(200, json=mock_resp_data)

    transport2 = httpx.MockTransport(mock_fallback_handler)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport2)):
        result = await _extract_with_router("Test prompt", "base64image", settings)
        assert result.amount == 42.50
        assert call_count == 2

    # Test SSE streaming response
    sse_lines = (
        'data: {"id":"1","choices":[{"delta":{"content":"{\\"date\\": \\"2026-10-26\\", \\"amount\\": 15.00,"}}]}\n\n'
        'data: {"id":"1","choices":[{"delta":{"content":" \\"store_name\\": \\"Bakery\\", \\"description\\": \\"Bread\\", \\"category\\": \\"Food\\", \\"budget\\": \\"Weekly\\"}"}}]}\n\n'
        'data: [DONE]\n\n'
    )

    async def mock_sse_handler(request: httpx.Request):
        return httpx.Response(
            200,
            text=sse_lines,
            headers={"content-type": "text/event-stream"},
        )

    transport_sse = httpx.MockTransport(mock_sse_handler)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport_sse)):
        result = await _extract_with_router("Test prompt", "base64image", settings)
        assert result.amount == 15.00
        assert result.store_name == "Bakery"

    # Test text-only extraction (no image)
    async def mock_text_handler(request: httpx.Request):
        body = json.loads(request.read())
        assert len(body["messages"][0]["content"]) == 1
        assert body["messages"][0]["content"][0]["type"] == "text"
        return httpx.Response(200, json=mock_resp_data)

    transport_text = httpx.MockTransport(mock_text_handler)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport_text)):
        result = await _extract_with_router("Text prompt", None, settings)
        assert result.amount == 42.50
        assert result.store_name == "Supermarket"

    print("_extract_with_router tests passed!")


if __name__ == "__main__":
    test_settings()
    test_models()
    test_date_and_json_parsing()
    asyncio.run(test_extract_with_router())
    print("\nALL TESTS PASSED SUCCESSFULLY!")
