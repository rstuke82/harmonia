# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS frontend
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-alpine3.23@sha256:1fd55c2f838c4f76262f9eeed899ff9168ea96ac383c61667d6f204325e1f169 AS runtime
ARG VERSION=1.0.0.102
ARG BUILD=102
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Harmonia" \
      org.opencontainers.image.description="Sync Spotify and Apple Music playlists to Plex" \
      org.opencontainers.image.source="https://github.com/rstuke82/harmonia" \
      org.opencontainers.image.version="${VERSION}" \
      io.harmonia.build="${BUILD}" \
      org.opencontainers.image.revision="${VCS_REF}"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HARMONIA_DATA_DIR=/config \
    HARMONIA_PORT=8173
WORKDIR /app
COPY requirements.txt requirements.lock ./
# Refresh the pinned base's OS packages and require the vendor's zlib fix.
# Keep native Python dependencies on published wheels; do not retain compilers.
RUN apk upgrade --no-cache && apk add --no-cache 'zlib>=1.3.2-r1' tzdata \
    && pip install --no-cache-dir --only-binary=:all: -r requirements.lock && pip uninstall -y pip
COPY harmonia/ ./harmonia/
COPY LICENSE ./
COPY scripts/container_entrypoint.py /usr/local/bin/harmonia-entrypoint.py
COPY --from=frontend /build/web/dist ./web/dist
RUN addgroup -g 10001 harmonia && adduser -D -H -u 10001 -G harmonia -s /sbin/nologin harmonia && mkdir -p /config && chown harmonia:harmonia /config && chmod 700 /config
EXPOSE 8173
VOLUME ["/config"]
ENTRYPOINT ["python", "/usr/local/bin/harmonia-entrypoint.py"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + (os.environ.get('HARMONIA_PORT') or '8173') + '/api/health', timeout=4).close()"]
CMD ["python", "-m", "harmonia", "web"]
