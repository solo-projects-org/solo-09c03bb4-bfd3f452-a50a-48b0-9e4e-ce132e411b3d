FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY birdapp ./birdapp
COPY run.py .

ENV BIRDAPP_DB=/data/birdapp.db
EXPOSE 8000

CMD ["python", "run.py"]
