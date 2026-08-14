FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLAWBOT_HOST=0.0.0.0 \
    CLAWBOT_PORT=18080 \
    CLAWBOT_POLICY_FILE=/app/config/policy.yaml \
    CLAWBOT_DB_PATH=/app/data/clawbot.db \
    CLAWBOT_MEDIA_ROOT=/app/data/media \
    CLAWBOT_EXPORT_ROOT=/app/data/exports

WORKDIR /app

COPY pyproject.toml requirements-build.lock requirements.lock README.md LICENSE ./
COPY src ./src
COPY config ./config
COPY examples ./examples
COPY openclaw ./openclaw
COPY workers ./workers
COPY demo ./demo
COPY evals ./evals
COPY policies ./policies

RUN pip install --no-cache-dir --requirement requirements-build.lock \
    --requirement requirements.lock \
    && pip install --no-cache-dir --no-build-isolation --no-deps .

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /app/data && chown -R app:app /app
USER app

EXPOSE 18080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18080/health', timeout=2)"

CMD ["dxl-commerce-agent"]
