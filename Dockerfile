FROM lscr.io/linuxserver/ffmpeg:8.1.2-cli-ls73

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip vainfo \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages \
    flask bcrypt waitress

ENV LIBVA_DRIVER_NAME=iHD
ENV PYTHONUNBUFFERED=1
ENV XDG_RUNTIME_DIR=/tmp
# Standardwerte – im docker-compose.yaml überschreibbar
ENV COOKIE_SECURE=0
ENV TRUST_PROXY=0

WORKDIR /app
COPY app/ /app/
RUN mkdir -p /config /tmp/mc_progress

EXPOSE 8686

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8686/api/auth-check')"

ENTRYPOINT []
CMD ["python3", "server.py"]
