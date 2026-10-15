FROM python:3.12-slim

# Controlador OpenCL de Intel para la gráfica integrada (UHD 630, generación 9.5): Debian 13 no lo
# empaqueta y la rama actual de Intel ya no cubre esa generación, así que va la "legacy1" oficial,
# verificada con su SHA-256. Sin la gráfica, el servicio sigue en CPU.
WORKDIR /tmp/neo
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates ocl-icd-libopencl1 >/dev/null \
 && B=https://github.com/intel/compute-runtime/releases/download/24.35.30872.22 \
 && G=https://github.com/intel/intel-graphics-compiler/releases/download/igc-1.0.17537.20 \
 && curl -fsSLO $G/intel-igc-core_1.0.17537.20_amd64.deb -fsSLO $G/intel-igc-opencl_1.0.17537.20_amd64.deb \
 && curl -fsSLO $B/intel-opencl-icd-legacy1_24.35.30872.22_amd64.deb -fsSLO $B/libigdgmm12_22.5.0_amd64.deb -fsSLO $B/ww35.sum \
 && grep -E "intel-opencl-icd-legacy1_|libigdgmm12_" ww35.sum | sha256sum -c - \
 && apt-get install -y -qq ./*.deb >/dev/null && rm -rf /tmp/neo /var/lib/apt/lists/*

WORKDIR /app
# PyTorch solo CPU: la imagen pesa la mitad; la gráfica se usa por OpenVINO, no por PyTorch.
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
