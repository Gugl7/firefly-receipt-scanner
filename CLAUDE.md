# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Receipt Scanner for Firefly III — a FastAPI web app that scans receipts using Google Gemini AI and creates transactions in Firefly III. Mobile-friendly with PWA support and camera capture.

## Commands

```bash
docker-compose up -d             # build and run on port 8000
docker-compose up -d --build     # rebuild after code changes
uv sync --frozen                 # install deps locally (for IDE support)
```

No test suite or linter is currently configured.

## Architecture

Three-tier FastAPI application with Jinja2 server-side rendering:

```
User → FastAPI (app.py) → Google Gemini AI (receipt_processing.py)
                        → Firefly III API (firefly.py)
                        → Image preprocessing (image_utils.py)
```

### Request Flow
1. `GET /` — upload form, fetches asset accounts from Firefly III
2. `POST /extract` — receives image + source account, processes image (resize/compress via `image_utils.py`), sends to Gemini with categories/budgets from Firefly III, returns review form
3. `POST /create-transaction` — validates via `ReceiptModel`, creates withdrawal in Firefly III with `#automated` tag, 3 retries with exponential backoff

### Key Modules
- **app.py** — FastAPI routes, middleware (ProxyHeaders, TrustedHost), static file mounting, startup validation of Firefly III connection
- **receipt_processing.py** — LLM integration supporting both 9Router (OpenAI-compatible) and direct Google Gemini AI, dynamic prompt construction with Firefly categories/budgets, structured JSON response via `ReceiptModel` schema
- **config.py** — Centralized configuration using `pydantic-settings`
- **firefly.py** — Firefly III REST API client (accounts, categories, budgets, transaction creation), 30s timeout, comprehensive HTTP error handling
- **image_utils.py** — PIL and pypdfium2 image/PDF processing: PDF conversion, RGB conversion, resize to max 768×768 with aspect ratio preservation
- **models.py** — `ReceiptModel` Pydantic model (date, amount, store_name, description, category, budget) with flexible currency/format parsing
- **templates/** — Jinja2 templates: `base.html` (layout + PWA meta), `upload.html`, `review.html`, `error.html`

### Configuration
Environment variables loaded via `pydantic-settings` (see `.env.example`):
- `FIREFLY_III_URL` — Firefly III instance base URL
- `FIREFLY_III_TOKEN` — Firefly III personal access token
- `LLM_PROVIDER` — LLM provider selection: `auto` (default), `9router` (or `openai`), or `gemini`
- `ROUTER_BASE_URL` (or `LLM_BASE_URL`) — 9Router API base URL (e.g. `http://localhost:20128/v1` or `http://host.docker.internal:20128/v1`)
- `ROUTER_API_KEY` (or `LLM_API_KEY`) — 9Router API key (optional/if enabled)
- `ROUTER_MODEL` (or `LLM_MODEL`) — Model name for 9Router (defaults to `GEMINI_MODEL` or `gemini-2.5-flash`)
- `GOOGLE_AI_API_KEY` — Google Gemini API key (required only when using direct Gemini)
- `GEMINI_MODEL` — Gemini model name (e.g. `gemini-2.5-flash`)
- `GEMINI_BASE_URL` — Optional custom base URL for Google Gemini SDK

### Tech Stack
Python 3.13, FastAPI, Uvicorn, Jinja2, Pydantic, Pillow, pypdfium2, google-genai, uv (package manager), Docker
