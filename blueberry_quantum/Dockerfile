FROM python:3.13-alpine
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY frontend/quantum-panel.js ./blueberry_quantum/public/quantum-panel.js
COPY BlueberryEero/provider.py ./BlueberryEero/provider.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "blueberry_quantum.server"]
