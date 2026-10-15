FROM python:3.12-slim

WORKDIR /app
# PyTorch solo CPU: la imagen pesa la mitad y el servidor no tiene GPU utilizable.
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# El modelo no va en la imagen: vive en el NAS (/modelos) y se descarga allí la primera vez.
# La caché de descarga va a /tmp: así el modelo queda una sola vez en el NAS.
ENV PYTHONUNBUFFERED=1 HF_HOME=/tmp/hf

COPY app.py .
EXPOSE 8001
HEALTHCHECK --interval=60s --timeout=5s --start-period=300s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/salud')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
