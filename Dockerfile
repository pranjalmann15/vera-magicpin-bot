FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUTF8=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
# Single worker on purpose: the bot keeps context + conversations in memory.
CMD ["sh", "-c", "uvicorn bot:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
