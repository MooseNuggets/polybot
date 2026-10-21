FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir cryptography==43.0.3
COPY . /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/polylog.db
CMD ["python", "logger.py"]
