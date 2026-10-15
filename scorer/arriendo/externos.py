"""Servicios externos: tiempo en transporte público (Transitous) y avisos (Telegram)."""

import base64
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("externos")

DESTINO = (4.5981, -74.0760)  # destino
BOGOTA = timezone(timedelta(hours=-5))
HTTP = httpx.Client(timeout=30, headers={"User-Agent": "arriendo-bot/0.1 (uso personal)"})


def proxima_salida() -> datetime:
    """Próximo día hábil a las 7:30 en Bogotá: la hora a la que se iría a la oficina."""
    dia = datetime.now(BOGOTA) + timedelta(days=1)
    while dia.weekday() >= 5:
        dia += timedelta(days=1)
    return dia.replace(hour=7, minute=30, second=0, microsecond=0)


def minutos_a_destino(lat: float, lon: float) -> int | None:
    """Duración del mejor itinerario en transporte público, o None si Transitous no responde."""
    try:
        r = HTTP.get("https://api.transitous.org/api/v1/plan", params={
            "fromPlace": f"{lat},{lon}", "toPlace": f"{DESTINO[0]},{DESTINO[1]}",
            "time": proxima_salida().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "numItineraries": 3})
        r.raise_for_status()
        duraciones = [it["duration"] for it in r.json().get("itineraries", [])]
        return round(min(duraciones) / 60) if duraciones else None
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("Transitous no respondió: %s", e)
        return None


def _arcgis_punto(url: str, lat: float, lon: float, **extra) -> dict | None:
    params = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
              "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false", "f": "json", **extra}
    try:
        r = HTTP.get(f"{url}/query", params=params)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Consulta a %s falló: %s", url, e)
        return None


def valor_suelo_m2(lat: float, lon: float) -> float | None:
    """Mediana del valor comercial del m² de terreno en la manzana (Catastro Bogotá)."""
    d = _arcgis_punto("https://serviciosgis.catastrobogota.gov.co/arcgis/rest/services/catastro/valorreferencia/MapServer/0",
                      lat, lon, outFields="V_REF,ANO")
    feats = (d or {}).get("features") or []
    return feats[0]["attributes"].get("V_REF") if feats else None


def siniestros_cerca(lat: float, lon: float, metros: int = 150) -> int | None:
    """Siniestros viales registrados a menos de `metros` (histórico 2015-2021)."""
    d = _arcgis_punto("https://services2.arcgis.com/NEwhEo9GGSHXcRXV/arcgis/rest/services/HistoricoSiniestros/FeatureServer/0",
                      lat, lon, spatialRel="esriSpatialRelIntersects", distance=metros, units="esriSRUnit_Meter",
                      returnCountOnly="true", where="1=1")
    return (d or {}).get("count")


VISION_URL = os.environ.get("VISION_URL", "")


def vision(ruta: str, cuerpo: dict, timeout: float = 600) -> dict | None:
    if not VISION_URL:
        return None
    try:
        r = httpx.post(f"{VISION_URL}{ruta}", json=cuerpo, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("El servicio de visión no respondió: %s", e)
        return None


def ruido_noche_db(lat: float, lon: float) -> float | None:
    """Ruido ambiental nocturno en día ordinario (Mapa Estratégico de Ruido 2018-2021, SDA): punto medio
    del rango en dB(A) del polígono que contiene el punto."""
    d = _arcgis_punto("https://mapas.ambientebogota.gov.co/server/rest/services/Calidad_auditiva/"
                      "MER_Nocturno_Jornada_Ordinario_dB_Bogota_DC__2018_2021/FeatureServer/0",
                      lat, lon, outFields="db_lo,db_hi")
    feats = (d or {}).get("features") or []
    if not feats:
        return None
    a = feats[0]["attributes"]
    try:  # el servicio publica los límites como texto
        return (float(a["db_lo"]) + float(a["db_hi"])) / 2
    except (KeyError, TypeError, ValueError):
        return None


NAVEGADOR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36", "Accept-Language": "es-CO,es;q=0.9"}
_ESTADO_CC = re.compile(r'<script id="[^"]*state[^"]*" type="application/json">(.*?)</script>', re.S)
_ENTIDADES_CC = {"&q;": '"', "&s;": "'", "&a;": "&", "&l;": "<", "&g;": ">"}


def foto_ciencuadras(url_s3: str) -> str:
    """Ciencuadras sirve las fotos por su redimensionador, con la ruta del bucket en base64."""
    clave = url_s3.split(".amazonaws.com/", 1)[-1]
    pedido = {"bucket": "www-img-cc", "key": clave, "edits": {"resize": {"width": 640, "height": 480, "fit": "inside"}}}
    return "https://images.ciencuadras.com/" + base64.b64encode(json.dumps(pedido, separators=(",", ":")).encode()).decode()


def pagina_ciencuadras(url: str) -> dict | None:
    """El estado que la página de un anuncio de Ciencuadras le pasa al navegador."""
    try:
        r = HTTP.get(url, headers=NAVEGADOR, follow_redirects=True)
        r.raise_for_status()
        m = _ESTADO_CC.search(r.text)
        estado = json.loads(re.sub(r"&[qsalg];", lambda x: _ENTIDADES_CC[x.group(0)], m.group(1))) if m else {}
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Detalle de Ciencuadras %s falló: %s", url, e)
        return None
    return next((v for k, v in estado.items() if k.startswith("detail-property-")), None)


def detalle_ciencuadras(url: str, pagina: dict | None = None) -> dict | None:
    """Descripción, galería y características de un anuncio de Ciencuadras: el listado solo trae la
    portada, sin texto ni detalles como el tipo de estufa o de piso."""
    detalle = pagina or pagina_ciencuadras(url)
    if not detalle:
        return None
    fotos = sorted((detalle.get("galleryData") or {}).get("flatPhotos") or [], key=lambda f: int(f.get("order") or 0))
    g = detalle.get("generalData") or {}
    rasgos = [v for k in ("stoveType", "typeFloors", "heaterType", "kitchenType", "diningRoomType", "view")
              if (v := g.get(k)) and "No tiene" not in str(v)]
    for campo, nombre in (("numElevators", "ascensor"), ("numBalconies", "balcón"), ("numTerrace", "terraza")):
        if re.search(r"[1-9]", str(g.get(campo) or "")):
            rasgos.append(nombre)
    return {"descripcion": g.get("description"), "facilidades": rasgos,
            "imagenes": [foto_ciencuadras(f["url"]) for f in fotos if f.get("url") and f.get("is360") != "1"][:20]}


def telegram(texto: str, foto: str | None = None) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.info("Telegram sin configurar; aviso solo en el log:\n%s", texto)
        return False
    base = f"https://api.telegram.org/bot{token}"
    try:
        if foto:
            r = HTTP.post(f"{base}/sendPhoto", data={"chat_id": chat, "photo": foto,
                                                     "caption": texto[:1024], "parse_mode": "HTML"})
            if r.is_success:
                return True
        r = HTTP.post(f"{base}/sendMessage", data={"chat_id": chat, "text": texto, "parse_mode": "HTML",
                                                   "disable_web_page_preview": "false"})
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.error("No se pudo avisar por Telegram: %s", e)
        return False


def e(texto) -> str:
    return html.escape(str(texto))
