FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Keep the application separate from the /app configuration volume.
WORKDIR /opt/recommendarr

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/recommendarr.py .

CMD ["python", "recommendarr.py", "--config", "/app/config.yaml", "--log-dir", "/app/logs", "--run-now"]
