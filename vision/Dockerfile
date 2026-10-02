FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MODEL_PATH=/opt/vitrine/model \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/app/vision/.cache/huggingface \
    TORCH_HOME=/app/vision/.cache/torch \
    XDG_CACHE_HOME=/app/vision/.cache \
    TMPDIR=/app/vision/.cache \
    TMP=/app/vision/.cache \
    TEMP=/app/vision/.cache

WORKDIR /app
RUN mkdir -p /app/vision/.cache \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY vision/requirements.txt /app/vision/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/vision/requirements.txt
COPY vision/__init__.py vision/detector.py vision/seed_model.py /app/vision/
RUN python -m vision.seed_model \
    && rm -rf /app/vision/.cache \
    && mkdir -p /app/vision/.cache

COPY vision/*.py /app/vision/
COPY vision/run-config.json /app/vision/run-config.json
COPY vision/licenses/ /app/vision/licenses/
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    ENVIRONMENT=azure \
    COSMOS_DATABASE=vitrine \
    COSMOS_CONTAINER=retail \
    AZURE_STORAGE_CONTAINER=retail

RUN groupadd --gid 10001 vitrine \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app/vision/.cache vitrine \
    && chown -R 10001:10001 /app/vision/.cache \
    && chmod -R a+rX /opt/vitrine/model
USER 10001:10001
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=3).close()"
CMD ["python", "-m", "vision.worker"]
