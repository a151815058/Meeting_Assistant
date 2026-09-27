FROM python:3.11-slim

WORKDIR /srv/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000
# Exactly one worker: recording state lives in process memory. Render passes the port in $PORT.
CMD ["sh", "-c", "exec gunicorn -k eventlet -w 1 -b 0.0.0.0:${PORT:-5000} run:app"]
