FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/

ENV DRNT_AUDIT_SOCKET=/var/drnt/sockets/audit.sock
ENV DRNT_AUDIT_DIR=/var/drnt/audit

CMD ["python", "-m", "src"]
