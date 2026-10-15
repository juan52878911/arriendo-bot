"""Servicio de visión: SigLIP calcula un vector por foto y unas cabezas pequeñas lo clasifican.

Todo lo pesado vive en el NAS: el modelo (/modelos/<nombre>, se descarga la primera vez), las cabezas
entrenadas (/modelos/cabezas.json) y las fotos (/fotos/<aa>/<huella>.webp, una sola vez cada una).

Cada foto lleva una huella visual (dHash de 64 bits) que no cambia al redimensionarla ni al
recomprimirla: así el scorer reconoce la misma foto en dos portales o subida dos veces.
"""

import io
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import numpy as np
import torch
import yaml
from fastapi import FastAPI
from PIL import Image
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from transformers import AutoProcessor, SiglipModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("vision")

CONFIG = Path(os.environ.get("ARRIENDO_CONFIG", "/config")) / "vision.yaml"
MODELOS = Path(os.environ.get("VISION_MODELOS", "/modelos"))
CABEZAS = MODELOS / "cabezas.json"
FOTOS = Path(os.environ.get("VISION_FOTOS", "/fotos"))
LADO_MAX = 1024        # las fotos se guardan a lo sumo a 1024 px por lado, en WebP calidad 80
# El redimensionador de fotos de Ciencuadras responde 404 a clientes que no se presentan como navegador.
HTTP = httpx.Client(timeout=30, follow_redirects=True, headers={
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"})
torch.set_num_threads(int(os.environ.get("VISION_HILOS", "3")))

estado: dict = {}


def cargar() -> None:
    cfg = yaml.safe_load(CONFIG.read_text())
    nombre = cfg["modelo"]
    local = MODELOS / nombre.replace("/", "__")
    if not (local / "config.json").exists():
        log.info("Descargando %s al NAS (%s), solo esta vez", nombre, local)
        SiglipModel.from_pretrained(nombre).save_pretrained(local)
        AutoProcessor.from_pretrained(nombre).save_pretrained(local)
    estado["modelo"] = SiglipModel.from_pretrained(local).eval()
    estado["proc"] = AutoProcessor.from_pretrained(local)
    estado["gpu"] = compilar_gpu(local)
    estado["cfg"] = cfg
    # Vectores de texto de cada clase, para el modo zero-shot.
    m = estado["modelo"]
    estado["escala"], estado["sesgo"] = float(m.logit_scale.exp()), float(m.logit_bias)
    estado["texto"] = {}
    for cabeza, c in cfg["cabezas"].items():
        frases = list(c["clases"].values())
        with torch.no_grad():
            t = estado["proc"](text=frases, padding="max_length", return_tensors="pt")
            v = estado["modelo"].get_text_features(**t)
        estado["texto"][cabeza] = (list(c["clases"].keys()), torch.nn.functional.normalize(v, dim=-1))
    estado["entrenadas"] = json.loads(CABEZAS.read_text()) if CABEZAS.exists() else {}
    log.info("Modelo %s listo; cabezas entrenadas: %s", nombre, list(estado["entrenadas"]))


class _Imagen(torch.nn.Module):
    """Solo la parte de imágenes de SigLIP, para exportarla a OpenVINO."""
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, pixel_values):
        return self.m.get_image_features(pixel_values=pixel_values)


def compilar_gpu(local: Path):
    """La gráfica integrada (Intel UHD 630, OpenVINO en f16) calcula los vectores el doble de rápido
    que la CPU con el mismo resultado (similitud 0,9999 en la prueba). Si no hay gráfica, None y se usa
    PyTorch en CPU. El modelo exportado se guarda en el NAS junto al original."""
    if os.environ.get("VISION_DISPOSITIVO", "auto") == "cpu":
        return None
    try:
        import openvino as ov
        core = ov.Core()
        if "GPU" not in core.available_devices:
            log.info("Sin gráfica para OpenVINO; visión en CPU")
            return None
        ir = local.parent / f"{local.name}-openvino" / "imagen.xml"
        if not ir.exists():
            ejemplo = estado["proc"](images=[Image.new("RGB", (224, 224))] * 2, return_tensors="pt")["pixel_values"]
            modelo = ov.convert_model(_Imagen(estado["modelo"]).eval(), example_input=ejemplo,
                                      input=[(-1, 3, 224, 224)])
            ir.parent.mkdir(parents=True, exist_ok=True)
            ov.save_model(modelo, ir, compress_to_fp16=True)
        compilado = core.compile_model(ir, "GPU", {"INFERENCE_PRECISION_HINT": "f16"})
        log.info("Visión en la gráfica integrada (%s)", core.get_property("GPU", "FULL_DEVICE_NAME"))
        return compilado
    except Exception:
        log.exception("No se pudo usar la gráfica; visión en CPU")
        return None


@asynccontextmanager
async def ciclo(_: FastAPI):
    cargar()
    yield


app = FastAPI(title="arriendo-bot vision", lifespan=ciclo)


def vectores(imagenes: list[Image.Image]) -> torch.Tensor:
    with torch.no_grad():
        x = estado["proc"](images=imagenes, return_tensors="pt")
        if estado.get("gpu") is not None:
            v = torch.from_numpy(estado["gpu"](x["pixel_values"].numpy())[0].astype(np.float32))
        else:
            v = estado["modelo"].get_image_features(**x)
        return torch.nn.functional.normalize(v, dim=-1)


def clasificar(cabeza: str, emb: np.ndarray) -> dict[str, float]:
    entrenada = estado["entrenadas"].get(cabeza)
    if entrenada:
        w, b = np.array(entrenada["w"]), np.array(entrenada["b"])
        logits = w @ emb + b
        clases = entrenada["clases"]
        if len(clases) == 2 and logits.shape == (1,):
            p = 1 / (1 + np.exp(-logits[0]))
            return {clases[0]: float(1 - p), clases[1]: float(p)}
    else:
        clases, texto = estado["texto"][cabeza]
        logits = emb @ texto.numpy().T * estado["escala"] + estado["sesgo"]
    e = np.exp(logits - logits.max())
    return {c: float(p) for c, p in zip(clases, e / e.sum())}


def huella(img: Image.Image) -> str:
    """dHash de 64 bits: compara cada píxel con su vecino en una miniatura de 9×8 en grises."""
    g = np.asarray(img.convert("L").resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"


def guardar_foto(h: str, img: Image.Image) -> str:
    """Guarda la foto en el NAS una sola vez; si ya existe una copia más pequeña, la reemplaza."""
    ruta = FOTOS / h[:2] / f"{h}.webp"
    if ruta.exists():
        with Image.open(ruta) as previa:
            if previa.width * previa.height >= min(img.width * img.height, LADO_MAX * LADO_MAX):
                return str(ruta.relative_to(FOTOS))
    ruta.parent.mkdir(parents=True, exist_ok=True)
    copia = img.copy()
    copia.thumbnail((LADO_MAX, LADO_MAX))
    tmp = ruta.with_suffix(".tmp")
    copia.save(tmp, "WEBP", quality=80, method=4)
    tmp.replace(ruta)  # atómico: nunca queda un archivo a medias
    return str(ruta.relative_to(FOTOS))


@app.post("/analizar")
def analizar(cuerpo: dict) -> dict:
    """{"urls": [...]} → por foto: huella, tamaño, archivo en el NAS, vector y probabilidades."""
    fotos, imagenes = [], []
    for url in cuerpo.get("urls", [])[: estado["cfg"]["fotos_por_inmueble"]]:
        try:
            r = HTTP.get(url)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as e:  # una foto rota no tumba el inmueble
            log.warning("Foto %s: %s", url, e)
            continue
        h = huella(img)
        imagenes.append(img)
        fotos.append({"url": url, "huella": h, "ancho": img.width, "alto": img.height,
                      "archivo": guardar_foto(h, img), "bytes": len(r.content)})
    if not imagenes:
        return {"fotos": []}
    embs = vectores(imagenes).numpy()
    for foto, emb in zip(fotos, embs):
        etiquetas = {"espacio": clasificar("espacio", emb), "render": clasificar("render", emb)}
        espacio = max(etiquetas["espacio"], key=etiquetas["espacio"].get)
        for cabeza, c in estado["cfg"]["cabezas"].items():
            if cabeza in etiquetas or espacio not in c.get("aplica_a", [espacio]):
                continue
            etiquetas[cabeza] = clasificar(cabeza, emb)
        foto.update(emb=[round(float(x), 5) for x in emb], espacio=espacio, etiquetas=etiquetas)
    return {"fotos": fotos}


@app.post("/borrar")
def borrar(cuerpo: dict) -> dict:
    """Borra del NAS archivos de fotos que el scorer ya no usa (copias más pequeñas, anuncios viejos)."""
    borrados = 0
    for rel in cuerpo.get("archivos", []):
        ruta = (FOTOS / rel).resolve()
        if FOTOS.resolve() in ruta.parents and ruta.suffix == ".webp" and ruta.exists():
            ruta.unlink()
            borrados += 1
    return {"borrados": borrados}


@app.post("/clasificar")
def clasificar_vectores(cuerpo: dict) -> dict:
    """Reclasifica vectores ya guardados (tras entrenar), sin volver a descargar fotos."""
    fuera = []
    for item in cuerpo.get("fotos", []):
        emb = np.array(item["emb"], dtype=np.float32)
        etiquetas = {"espacio": clasificar("espacio", emb), "render": clasificar("render", emb)}
        espacio = max(etiquetas["espacio"], key=etiquetas["espacio"].get)
        for cabeza, c in estado["cfg"]["cabezas"].items():
            if cabeza not in etiquetas and espacio in c.get("aplica_a", [espacio]):
                etiquetas[cabeza] = clasificar(cabeza, emb)
        fuera.append({"url": item["url"], "espacio": espacio, "etiquetas": etiquetas})
    return {"fotos": fuera}


@app.post("/entrenar")
def entrenar(cuerpo: dict) -> dict:
    """{"ejemplos": {cabeza: [{"emb": [...], "clase": "si"}, ...]}} → métricas por cabeza.

    Una cabeza entrenada reemplaza al zero-shot solo si su exactitud en validación cruzada supera
    la del zero-shot sobre esas mismas etiquetas.
    """
    informe, entrenadas = {}, dict(estado["entrenadas"])
    for cabeza, ejemplos in cuerpo.get("ejemplos", {}).items():
        if cabeza not in estado["cfg"]["cabezas"]:
            continue
        X = np.array([e["emb"] for e in ejemplos], dtype=np.float32)
        y = np.array([e["clase"] for e in ejemplos])
        clases = sorted(set(y))
        minimo = min((y == c).sum() for c in clases) if clases else 0
        if len(clases) < 2 or minimo < 5:
            informe[cabeza] = {"estado": "faltan etiquetas", "n": len(y), "por_clase_min": int(minimo)}
            continue
        estado["entrenadas"].pop(cabeza, None)  # medir el zero-shot puro
        zs = float(np.mean([max(clasificar(cabeza, x), key=clasificar(cabeza, x).get) == c for x, c in zip(X, y)]))
        clf = LogisticRegression(max_iter=2000, C=1.0, class_weight="balanced")
        cv = float(cross_val_score(clf, X, y, cv=min(5, minimo)).mean())
        clf.fit(X, y)
        if cv > zs:
            entrenadas[cabeza] = {"clases": [str(c) for c in clf.classes_], "w": clf.coef_.tolist(),
                                  "b": clf.intercept_.tolist(), "exactitud": cv, "n": len(y)}
            decision = "entrenada"
        else:
            entrenadas.pop(cabeza, None)
            decision = "zero-shot (acierta más)"
        informe[cabeza] = {"estado": decision, "n": len(y), "zero_shot": round(zs, 3), "entrenada_cv": round(cv, 3)}
    estado["entrenadas"] = entrenadas
    CABEZAS.parent.mkdir(parents=True, exist_ok=True)
    CABEZAS.write_text(json.dumps(entrenadas))
    return informe


@app.get("/salud")
def salud() -> dict:
    return {"ok": "modelo" in estado, "entrenadas": list(estado.get("entrenadas", {})),
            "dispositivo": "gpu" if estado.get("gpu") is not None else "cpu"}
