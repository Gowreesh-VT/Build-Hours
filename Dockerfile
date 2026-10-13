# Alternative to the Python runtime on Render (or any Docker host).
#   docker build -t gesture-relay .
#   docker run -p 8765:8765 -e EVENT_TOKEN=secret gesture-relay
FROM python:3.12-slim

WORKDIR /app
COPY requirements-relay.txt .
RUN pip install --no-cache-dir -r requirements-relay.txt
COPY relay.py .

ENV PYTHONUNBUFFERED=1 \
    PORT=8765
EXPOSE 8765
CMD ["python", "relay.py"]
