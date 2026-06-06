# Usar la imagen oficial de Playwright que ya incluye Python 3.10
FROM mcr.microsoft.com/playwright:python

WORKDIR /app

# Copiar archivo de dependencias
COPY requirements.txt .

# Instalar dependencias Python (pip ya existe)
RUN pip install --no-cache-dir -r requirements.txt

# Copiar toda la aplicación
COPY . .

# Asegurar que Firefox esté instalado (por si acaso)
RUN playwright install firefox

# Exponer puerto
EXPOSE 8080

# Variables de entorno
ENV HEADLESS=true
ENV PORT=8080

# Comando para iniciar
CMD ["python", "server.py"]