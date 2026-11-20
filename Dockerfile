FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

ENV DB_PATH=/data/markrecap.db \
    PORT=8000

RUN useradd -r -u 10001 appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "app:app"]
