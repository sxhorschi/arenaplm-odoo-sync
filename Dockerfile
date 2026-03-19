FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PRODUCTION=1 \
    PORT=5000 \
    DATA_DIR=/app/data

RUN adduser --disabled-password --no-create-home appuser \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data

USER appuser

EXPOSE ${PORT}

ENTRYPOINT ["python", "main.py"]
