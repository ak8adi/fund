FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fundus ./fundus
RUN pip install --no-cache-dir .

ENV FUNDUS_CONFIG=/data/config.yaml \
    FUNDUS_DATA_DIR=/data \
    FUNDUS_WEB_HOST=0.0.0.0 \
    TZ=Europe/Zurich
VOLUME /data
EXPOSE 8100
CMD ["fundus", "serve"]
