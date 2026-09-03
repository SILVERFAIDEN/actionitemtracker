FROM python:3.14-slim

WORKDIR /srv/tracker

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

WORKDIR /srv/tracker/app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
