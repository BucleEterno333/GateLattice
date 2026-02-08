FROM python:3.9-slim

WORKDIR /app

# Instalar dependencias esenciales
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    libnss3 \
    libxss1 \
    libxtst6 \
    libasound2 \
    libgbm1 \
    libx11-xcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Instalar Chromium desde repositorio de Debian
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-common \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Playwright sin instalar browsers (usaremos Chromium del sistema)
RUN pip install playwright==1.40.0

# Configurar Playwright para usar Chromium del sistema
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV CHROMIUM_BIN=/usr/bin/chromium

# Copiar aplicación
COPY . .

# Exponer puerto
EXPOSE 8080

# Comando para iniciar
CMD ["python", "server.py"]