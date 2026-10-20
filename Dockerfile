FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ROOMLOOP_DB=/data/roomloop.db

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY roomloop ./roomloop
COPY data ./data

RUN useradd --create-home --uid 10001 roomloop \
    && mkdir -p /data && chown -R roomloop:roomloop /data
USER roomloop

EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["uvicorn", "roomloop.api:app", "--host", "0.0.0.0", "--port", "8000"]
