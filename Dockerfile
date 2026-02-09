FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY run.py /app/run.py

# If you commit places.txt in git, keep this line.
# If you prefer to keep it outside git, we'll mount it via docker-compose.
COPY places.txt /app/places.txt

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir flask requests gunicorn

# Gunicorn listens on container port 8000
EXPOSE 8000

# run:app matches your file run.py (app = Flask(__name__))
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "run:app"]
