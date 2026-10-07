ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PLAYWRIGHT_DOWNLOAD_HOST=
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /app
COPY backend/requirements.lock /app/backend/requirements.lock
RUN python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r backend/requirements.lock \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY backend /app/backend
COPY adapters /app/adapters
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && chmod -R a+rX /app/backend /app/adapters \
    && mkdir -p /data/artifacts /data/browser-profiles /data/beat \
    && chown -R app:app /data /opt/playwright
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
