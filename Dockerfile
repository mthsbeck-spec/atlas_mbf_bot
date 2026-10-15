FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=America/Sao_Paulo
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY assistente ./assistente
RUN mkdir -p /data
ENV DATA_DIR=/data
CMD ["python", "-m", "assistente"]
