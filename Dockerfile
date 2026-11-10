FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Sao_Paulo

WORKDIR /app

# A aplicação usa zoneinfo para datas no fuso de São Paulo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" capoeira \
    && useradd --uid "${APP_UID}" --gid capoeira \
        --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin capoeira \
    && mkdir -p /app/instance \
    && chown -R capoeira:capoeira /app

COPY --chown=capoeira:capoeira . .

USER capoeira
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:5000/', timeout=3)" || exit 1

CMD ["waitress-serve", "--listen=0.0.0.0:5000", "--threads=8", "app:app"]
