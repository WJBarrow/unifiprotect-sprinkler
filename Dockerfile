FROM python:3.11-slim

WORKDIR /app
COPY sprinkler.py .

EXPOSE 8383

CMD ["python", "-u", "sprinkler.py"]
