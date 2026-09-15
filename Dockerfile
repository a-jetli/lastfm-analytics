FROM python:3.12-slim

WORKDIR /app

# requirements first, so code edits do not invalidate the installed layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

# 0.0.0.0, not 127.0.0.1, so it is reachable from outside the container
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
