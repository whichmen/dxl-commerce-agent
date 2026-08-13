FROM python:3.14.0-slim@sha256:0aecac02dc3d4c5dbb024b753af084cafe41f5416e02193f1ce345d671ec966e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DXL_DATABASE_PATH=/app/data/demo.db \
    DXL_FIXTURE_DIR=/app/demo/fixtures \
    DXL_POLICY_PATH=/app/policies/default.toml

WORKDIR /app

COPY pyproject.toml requirements-build.lock requirements.lock README.md LICENSE ./
COPY src ./src
COPY demo ./demo
COPY policies ./policies

RUN pip install --no-cache-dir --requirement requirements-build.lock \
    --requirement requirements.lock \
    && pip install --no-cache-dir --no-build-isolation --no-deps .

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /app/data && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["uvicorn", "dxl_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
