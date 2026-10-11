# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS frontend
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
ARG VERSION=1.0.0.75
ARG BUILD=75
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
RUN pip install --no-cache-dir -r requirements.lock && pip uninstall -y pip
COPY playlist_bridge/ ./playlist_bridge/
COPY LICENSE ./
COPY scripts/container_entrypoint.py /usr/local/bin/harmonia-entrypoint.py
COPY --from=frontend /build/web/dist ./web/dist
RUN groupadd --gid 10001 harmonia && useradd --uid 10001 --gid harmonia --no-create-home --shell /usr/sbin/nologin harmonia && mkdir -p /config && chown harmonia:harmonia /config && chmod 700 /config
EXPOSE 8173
VOLUME ["/config"]
ENTRYPOINT ["python", "/usr/local/bin/harmonia-entrypoint.py"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + (os.environ.get('HARMONIA_PORT') or '8173') + '/api/health', timeout=4).close()"]
CMD ["python", "-m", "playlist_bridge", "web"]
