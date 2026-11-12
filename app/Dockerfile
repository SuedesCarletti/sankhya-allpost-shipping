# Use uma imagem base oficial do Python
FROM python:3.9-slim

# Define o diretório de trabalho dentro do container
WORKDIR /app

# Copia o arquivo de dependências para o diretório de trabalho
COPY requirements.txt .

# Instala as dependências definidas no requirements.txt
# A flag --no-cache-dir reduz o tamanho da imagem
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do código da aplicação para o diretório de trabalho
COPY . .

# Expõe a porta que o Gunicorn irá usar
EXPOSE 5001

# Define a variável de ambiente PORT, que será usada pela aplicação
ENV PORT=5001

# Comando para iniciar a aplicação usando Gunicorn
# `app:app` refere-se ao objeto Flask `app` criado em `app.py`
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "2", "--threads", "4", "app:app"]
