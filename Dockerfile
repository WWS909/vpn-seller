FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY web ./web
COPY scripts/tune.sh scripts/ru-probe.py ./scripts/
COPY VERSION CHANGELOG.md CHANGELOG.en.md ./
COPY deploy/entrypoint.sh /entrypoint.sh

# The server runs as an unprivileged user. The entrypoint starts as root
# only to make the mounted /app/data (database, verification files)
# writable for it, then drops privileges.
RUN useradd --uid 10001 --user-group --no-create-home app \
 && mkdir -p /app/data && chown app:app /app/data \
 && chmod 755 /entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
