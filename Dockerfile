FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=America/Sao_Paulo
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /src
# Aceita o repositório com a pasta assistente/ (estrutura original) ou com os .py soltos na raiz
RUN set -e; \
    if [ -f /src/assistente/agente.py ]; then cp -r /src/assistente /app/assistente; \
    elif [ -f /src/agente.py ]; then mkdir /app/assistente && cp /src/*.py /app/assistente/; \
    else echo "ERRO: não achei agente.py. Envie ao GitHub a pasta assistente/ com os arquivos .py" && ls -R /src && exit 1; fi; \
    pip install --no-cache-dir -r /src/requirements.txt; \
    python -c "import assistente.bot" && rm -rf /src && mkdir -p /data
ENV DATA_DIR=/data
CMD ["python", "-c", "from assistente.bot import iniciar; iniciar()"]
