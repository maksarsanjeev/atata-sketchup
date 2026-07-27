FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ATATA_DATA_DIR=/data

WORKDIR /app

# Pillow из wheel тянет свои библиотеки, но libjpeg/zlib нужны для экзотики
# вроде TIFF внутри .skp — они там встречаются.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libjpeg62-turbo zlib1g libtiff6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY atata/ ./atata/

RUN useradd -m -u 10001 atata && mkdir -p /data && chown -R atata:atata /data /app
USER atata

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# Один воркер: анализ .skp упирается в CPU и держит факты в памяти процесса.
CMD ["uvicorn", "atata.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
