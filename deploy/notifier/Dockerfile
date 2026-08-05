FROM python:3.12-slim
WORKDIR /app
COPY frigate_notifier.py /app/frigate_notifier.py
COPY notification_clients.py /app/notification_clients.py
COPY media_gateway.py /app/media_gateway.py
CMD ["python", "/app/frigate_notifier.py"]
