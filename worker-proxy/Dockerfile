FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 9100

# timeout-keep-alive must exceed maximum wall_timeout (300s) + buffer
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9100", "--timeout-keep-alive", "360"]
