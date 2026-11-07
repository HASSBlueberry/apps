FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends adb ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "blueberry_qdb.server"]
