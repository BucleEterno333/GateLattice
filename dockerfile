# Usa la imagen oficial de Playwright (basada en Ubuntu, con todo preinstalado)
FROM mcr.microsoft.com/playwright:latest

WORKDIR /app

# Copiar archivo de dependencias Python
COPY requirements.txt .

# Instalar dependencias Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto de la aplicación
COPY . .

# Opcional: Si quieres asegurar que Firefox está disponible (ya lo trae la imagen)
# Pero por si acaso, forzamos la instalación del browser específico
RUN playwright install firefox

# Exponer el puerto que usa tu servidor Flask
EXPOSE 8080

# Variables de entorno por defecto (puedes sobreescribirlas al correr)
ENV HEADLESS=true
ENV PORT=8080

# Comando para iniciar la aplicación
CMD ["python", "server.py"]