# Headless CLI image. The browser GUI stays loopback-only and is not exposed here.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --requirement requirements.txt

COPY proxytool.py anonymity.py branding.py maintenance.py reputation.py sources.json service.example.json ./
RUN useradd --create-home --uid 10001 workbench \
    && mkdir -p /app/data \
    && chown workbench /app/data

USER workbench
VOLUME ["/app/data"]
ENTRYPOINT ["python", "proxytool.py"]
CMD ["--help"]
