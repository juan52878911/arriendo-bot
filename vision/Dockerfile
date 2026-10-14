FROM python:3.12-slim

WORKDIR /app
# PyTorch solo CPU: la imagen pesa la mitad y el servidor no tiene GPU utilizable.
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# El modelo se descarga al construir, no al arrancar: el servicio no depende de Hugging Face en marcha.
ARG MODELO=google/siglip-base-patch16-224
RUN python -c "from transformers import AutoProcessor, SiglipModel as M; M.from_pretrained('$MODELO').save_pretrained('/modelo'); AutoProcessor.from_pretrained('$MODELO').save_pretrained('/modelo')"
ENV VISION_MODELO_LOCAL=/modelo PYTHONUNBUFFERED=1

COPY app.py .
EXPOSE 8001
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/salud')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
