FROM python:3.12-slim

WORKDIR /app

# Print log lines immediately instead of buffering them.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY config.yaml ./

CMD ["python", "main.py"]
