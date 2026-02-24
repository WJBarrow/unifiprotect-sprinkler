FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir websocket-client
COPY sprinkler.py .

EXPOSE 8383

CMD ["python", "-u", "sprinkler.py"]
