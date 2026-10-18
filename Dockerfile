FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/polylog.db
CMD ["python", "logger.py"]
