FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=medvolt_analytics.settings

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home /app app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN sed -i 's/\r$//' /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh \
    && mkdir -p /app/data /app/staticfiles \
    && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=3)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "medvolt_analytics.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
