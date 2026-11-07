FROM python:3.13-slim-bookworm
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY runtime/ /runtime/
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "blueberry_droid.server"]
