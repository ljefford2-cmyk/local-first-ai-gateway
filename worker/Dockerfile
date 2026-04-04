FROM python:3.12-slim

COPY worker_agent.py /usr/local/bin/worker_agent.py

RUN mkdir /inbox /outbox \
    && adduser --disabled-password --gecos "" --no-create-home drnt \
    && chown drnt:drnt /inbox /outbox

USER drnt

ENTRYPOINT ["python", "/usr/local/bin/worker_agent.py"]
