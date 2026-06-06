FROM mcr.microsoft.com/playwright:v1.40.0-focal

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Forzar instalación de Firefox (por si la imagen no lo tuviera, aunque debería)
RUN playwright install firefox

# Copiar toda la app
COPY . .

# Variables de entorno
ENV HEADLESS=true
ENV PORT=8080

EXPOSE 8080

CMD ["python", "server.py"]