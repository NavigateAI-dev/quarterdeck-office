FROM ghcr.io/astral-sh/uv:0.10.11@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4 AS uv
FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS runtime
COPY --from=uv /uv /usr/local/bin/uv
# git checks a coding task's result in a clone of its own; Node runs builds'
# checks and npm installs their packages (the installer service); Tesseract
# reads scanned PDFs and pictures, in English and Chinese,
# in the no-network sources service. No coding CLI: since Stage 36 coding runs
# on the owner's Mac, by Quarterdeck Coder (scripts/mac_coder.py).
RUN apt-get update \
    && apt-get install --no-install-recommends --yes git ca-certificates curl gnupg \
       tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install --no-install-recommends --yes nodejs \
    && npm cache clean --force \
    && apt-get purge --yes gnupg && apt-get autoremove --yes \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./
RUN groupadd --gid 10001 office && useradd --uid 10001 --gid office --no-create-home office \
    && mkdir /workspace /engineering /sources-exchange /exchange /fetch-exchange /remote-exchange /browser-exchange \
    && chown office:office /workspace /engineering /sources-exchange /exchange /fetch-exchange /remote-exchange /browser-exchange
ENV PATH="/app/.venv/bin:$PATH"
USER office
EXPOSE 8000
# Open pages keep a live stream (Stage 5b); on shutdown they are closed after
# 5 s instead of holding the restart. The browser reconnects by itself.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "5"]

# The office's own browser (CU1): the runtime plus Playwright and Chromium,
# and nothing the office holds. Built only for the browser service.
FROM runtime AS browser
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN uv sync --frozen --no-dev --group browser \
    && /app/.venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R o+rx /ms-playwright
USER office
CMD ["python", "-m", "app.browser.service"]

# Test image: the same runtime plus development dependencies. Tests that need
# git run here, because git is installed in the worker environment.
FROM runtime AS test
USER root
RUN uv sync --frozen
COPY tests ./tests
# Deployment configuration is asserted by tests; these files carry no secrets.
COPY docker-compose.yml .env.example Dockerfile ./
RUN chown -R office:office /app
USER office
CMD ["pytest", "-q", "--tb=short"]
