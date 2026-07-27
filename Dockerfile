FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ATATA_DATA_DIR=/data \
    WINEDEBUG=-all \
    WINEPREFIX=/wine \
    ATATA_WINE=/usr/lib/wine/wine64 \
    ATATA_WINE_PYTHON=/opt/winpython/python.exe

WORKDIR /app

# Pillow из wheel тянет свои библиотеки, но libjpeg/zlib нужны для экзотики
# вроде TIFF внутри .skp — она там встречается.
#
# wine64 нужен для слоя геометрии: SketchUp SDK существует только под
# Windows и macOS. Проверено на файле 348 МБ — под Wine результат совпадает
# с нативным прогоном до единицы, скорость примерно в 1.8 раза ниже.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g libtiff6 \
        wine64 libwine \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Windows-питон для воркера SDK: обвязка — чистый ctypes, поэтому хватает
# embeddable-сборки без единой сторонней зависимости.
RUN curl -sL -o /tmp/winpython.zip \
        https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/winpython.zip').extractall('/opt/winpython')" \
    && rm /tmp/winpython.zip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY atata/ ./atata/

# Воркер запускается Windows-питоном, который ищет пакеты рядом с собой,
# поэтому пакет кладётся и туда. Ссылкой, чтобы образ не толстел вдвое.
RUN ln -s /app/atata /opt/winpython/atata

RUN useradd -m -u 10001 atata \
    && mkdir -p /data /wine \
    && chown -R atata:atata /data /wine /app
USER atata

# Префикс Wine создаётся заранее, иначе первая же задача потратит на это
# несколько секунд и словит гонку при параллельных запусках.
RUN wine64 wineboot --init 2>/dev/null; sleep 2; true

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# Один воркер uvicorn: анализ .skp упирается в CPU и держит факты в памяти процесса.
CMD ["uvicorn", "atata.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
