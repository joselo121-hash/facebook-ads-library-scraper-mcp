FROM python:3.12-slim

# crawl4ai (Playwright) needs a real headless Chromium plus its OS-level libs. Using
# "playwright install --with-deps" (not a pinned mcr.microsoft.com/playwright/python
# base image) because requirements.txt doesn't pin an exact playwright version — this
# installs whatever version actually resolves at build time, not a possibly-mismatched
# pre-baked browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY facebook_ads_mcp_complete.py security_http.py ./

ENV MCP_TRANSPORT=streamable-http
ENV MCP_HTTP_HOST=0.0.0.0
ENV MCP_HTTP_PORT=8800
ENV MCP_HTTP_PATH=/mcp
ENV PYTHONUNBUFFERED=1

EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://127.0.0.1:8800/healthz || exit 1

CMD ["python", "facebook_ads_mcp_complete.py"]
