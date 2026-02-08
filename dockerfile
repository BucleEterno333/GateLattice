FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistemaa
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements primero para caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto de la aplicación
COPY . .

# Crear directorios necesarios
RUN mkdir -p static templates logs

# Variables de entorno por defecto
ENV FLASK_APP=server.py
ENV FLASK_ENV=production
ENV PORT=8080
ENV MAX_WORKERS=5
ENV API_TIMEOUT=30
ENV LOG_LEVEL=INFO

# Exponer puerto
EXPOSE 8080

# Comando para ejecutar
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "4", "--threads", "2", "--timeout", "60", "server:app"]