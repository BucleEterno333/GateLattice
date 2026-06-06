FROM mcr.microsoft.com/playwright:focal

WORKDIR /app

# Instalar Python y pip
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*

# Crear enlace simbólico para 'python'
RUN ln -s /usr/bin/python3 /usr/bin/python

# Copiar requirements y instalar
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copiar código
COPY . .

# Instalar Firefox (ya debería estar, pero por si)
RUN playwright install firefox

EXPOSE 8080
ENV HEADLESS=true
ENV PORT=8080

CMD ["python", "server.py"]