FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.deploy.txt .
RUN pip install --no-cache-dir -r requirements.deploy.txt

COPY server.py ./server.py
COPY db.py ./db.py
COPY openai_translator.py ./openai_translator.py
COPY logger.py ./logger.py
COPY admin_cli.py ./admin_cli.py
COPY verify_openai.py ./verify_openai.py
COPY static ./static

EXPOSE 8800

CMD ["python", "server.py"]
