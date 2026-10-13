FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Container stays alive; the actual sync is triggered on a schedule by Ofelia
# (see compose labels) or manually:
#   docker exec plex-arr-date-sync python3 /app/sync.py
CMD ["sleep", "infinity"]
