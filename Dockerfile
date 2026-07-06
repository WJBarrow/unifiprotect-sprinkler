FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sprinkler.py .

EXPOSE 8383

CMD ["python", "-u", "sprinkler.py"]
