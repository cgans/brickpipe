# brickpipe's page, for hosting next to LDraw Nova (see docs/hosting.md).
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY brickpipe ./brickpipe
RUN pip install --no-cache-dir . && mkdir -p /data/runs
# Everything brickpipe keeps lives under /data: runs, the LDraw library it
# downloads, and the Rebrickable cache. Mount a volume there.
ENV XDG_DATA_HOME=/data XDG_CACHE_HOME=/data/cache \
    BRICKPIPE_HOST=0.0.0.0 BRICKPIPE_PORT=8766 BRICKPIPE_RUNS_DIR=/data/runs \
    BRICKPIPE_NO_BROWSER=1 BRICKPIPE_LOCKED=1 BRICKPIPE_GET_LDRAW=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8766
CMD ["brickpipe-ui"]
