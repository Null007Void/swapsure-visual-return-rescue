# SwapSure 1.7.12 - application image for Google Cloud (Compute Engine with a persistent disk).
# The app binds loopback only; TLS and authentication are the proxy job (see deploy/Caddyfile).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY pyproject.toml README.md LICENSE THIRD-PARTY-NOTICES.md ./
COPY src ./src
COPY contracts ./contracts
COPY docs ./docs
COPY fixtures ./fixtures
COPY assets ./assets
COPY tools ./tools

RUN pip install --no-cache-dir "Pillow>=10,<13"

EXPOSE 8080
# Data lives on a mounted disk so SQLite accounting and uploaded media survive restarts.
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=3)"
CMD ["python", "src/ui/server.py", "--port", "8080", "--data-dir", "/data", "--preview-mode", "LIVE", "--bind", "127.0.0.1", "--threaded"]
