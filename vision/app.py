"""Servicio de visión: SigLIP calcula un vector por foto y unas cabezas pequeñas lo clasifican.

Sin estado salvo las cabezas entrenadas (/modelos/cabezas.json). El scorer guarda los vectores, así
que entrenar una cabeza nueva no exige volver a procesar ninguna foto.
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
CABEZAS = Path(os.environ.get("VISION_MODELOS", "/modelos")) / "cabezas.json"
HTTP = httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "arriendo-bot/0.1 (uso personal)"})
torch.set_num_threads(int(os.environ.get("VISION_HILOS", "3")))

estado: dict = {}


def cargar() -> None:
    cfg = yaml.safe_load(CONFIG.read_text())
    nombre = os.environ.get("VISION_MODELO_LOCAL", cfg["modelo"])
    estado["modelo"] = SiglipModel.from_pretrained(nombre).eval()
    estado["proc"] = AutoProcessor.from_pretrained(nombre)
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


@asynccontextmanager
async def ciclo(_: FastAPI):
    cargar()
    yield


app = FastAPI(title="arriendo-bot vision", lifespan=ciclo)


def vectores(imagenes: list[Image.Image]) -> torch.Tensor:
    with torch.no_grad():
        x = estado["proc"](images=imagenes, return_tensors="pt")
        return torch.nn.functional.normalize(estado["modelo"].get_image_features(**x), dim=-1)


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


@app.post("/analizar")
def analizar(cuerpo: dict) -> dict:
    """{"urls": [...]} → por foto: vector y probabilidades de cada cabeza que aplique."""
    fotos, imagenes = [], []
    for url in cuerpo.get("urls", [])[: estado["cfg"]["fotos_por_inmueble"]]:
        try:
            r = HTTP.get(url)
            r.raise_for_status()
            imagenes.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
            fotos.append({"url": url})
        except Exception as e:  # una foto rota no tumba el inmueble
            log.warning("Foto %s: %s", url, e)
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
    return {"ok": "modelo" in estado, "entrenadas": list(estado.get("entrenadas", {}))}
