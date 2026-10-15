"""Lector de los correos de alerta de los portales (búsquedas guardadas en Fincaraíz, Metrocuadrado y
Ciencuadras). Es la vía de respaldo: si un portal bloquea la lectura de sus listados, sus correos
siguen llegando.

Configuración (.env): IMAP_USER, IMAP_PASSWORD (en Gmail, una "contraseña de aplicación", no la
normal), y opcionales IMAP_HOST (imap.gmail.com) e IMAP_CARPETA (INBOX). Solo lee: nunca marca,
mueve ni borra correos.
"""

import email
import imaplib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape

from . import externos

log = logging.getLogger("correo")

REMITENTES = {"fincaraiz": "fincaraiz.com.co", "metrocuadrado": "metrocuadrado.com", "ciencuadras": "ciencuadras.com"}
# Enlaces a un anuncio de arriendo, ya sin seguimiento.
ANUNCIO = {
    "fincaraiz": re.compile(r"https://www\.fincaraiz\.com\.co/[^\s\"'<>?#]*arriendo[^\s\"'<>?#]*/(\d{6,})"),
    "metrocuadrado": re.compile(r"https://www\.metrocuadrado\.com/inmueble/arriendo-[^\s\"'<>?#]+/([\w-]+-M\d+)"),
    "ciencuadras": re.compile(r"https://www\.ciencuadras\.com/inmueble/[^\s\"'<>?#]*arriendo[^\s\"'<>?#]*-(\d{6,})"),
}
ENLACE = re.compile(r"https?://[^\s\"'<>]+")
MAX_REDIRECCIONES = 30  # por correo: los boletines traen muchos enlaces de seguimiento


def configurado() -> bool:
    return bool(os.environ.get("IMAP_USER") and os.environ.get("IMAP_PASSWORD"))


def _texto(msg: email.message.Message) -> str:
    partes = []
    for parte in msg.walk():
        if parte.get_content_type() in ("text/html", "text/plain"):
            carga = parte.get_payload(decode=True) or b""
            partes.append(carga.decode(parte.get_content_charset() or "utf-8", errors="replace"))
    return unescape("\n".join(partes))


def enlaces_de_anuncios(texto: str, portal: str) -> set[str]:
    """URLs canónicas de anuncios en el correo. Los enlaces de seguimiento del propio portal se
    siguen (una petición cada uno, con tope) para llegar a la URL real."""
    directos = {m.group(0) for m in ANUNCIO[portal].finditer(texto)}
    seguidos = 0
    for url in ENLACE.findall(texto):
        if seguidos >= MAX_REDIRECCIONES or ANUNCIO[portal].match(url):
            continue
        dominio = re.sub(r"https?://([^/]+).*", r"\1", url)
        if portal not in dominio or dominio.startswith("www."):
            continue  # solo redirecciones del portal (click.…, links.…), nunca terceros
        try:
            final = str(externos.HTTP.get(url, headers=externos.NAVEGADOR, follow_redirects=True).url)
        except Exception:
            continue
        seguidos += 1
        if m := ANUNCIO[portal].match(final):
            directos.add(m.group(0))
    return directos


# Detalle de un anuncio que llegó por correo: se lleva al mismo formato que el listado del portal.

def _next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else {}


def _flight(html: str) -> str:
    return "".join(json.loads(f'"{c}"') for c in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S))


def registro_fincaraiz(url: str) -> dict | None:
    r = externos.HTTP.get(url, headers=externos.NAVEGADOR, follow_redirects=True)
    data = (_next_data(r.text).get("props") or {}).get("pageProps", {}).get("data")
    return data if data and data.get("id") else None


def registro_metrocuadrado(url: str) -> dict | None:
    r = externos.HTTP.get(url, headers=externos.NAVEGADOR, follow_redirects=True)
    flight = _flight(r.text)
    i = flight.find('{"data":{"propertyId"')
    if i < 0:
        return None
    d = json.JSONDecoder().raw_decode(flight[i:])[0]["data"]
    rasgos = [f"{x}:S" for grupo in d.get("featured") or [] for x in grupo.get("items") or []]
    return {
        "midinmueble": d["propertyId"], "title": d.get("title"), "link": d.get("link", "").replace("https://www.metrocuadrado.com", ""),
        "mvalorarriendo": d.get("rentPrice"), "data": {"mvaloradministracion": (d.get("detail") or {}).get("adminPrice")},
        "areaprivada": d.get("areaPrivada"), "marea": d.get("area"), "mareac": d.get("areac"),
        "mnrocuartos": d.get("rooms"), "mnrobanos": d.get("bathrooms"), "mnrogarajes": d.get("garages"),
        "estrato": int(d["stratum"]) if str(d.get("stratum") or "").isdigit() else None,
        "localizacion": d.get("coordinates"), "comment": d.get("comment"), "featured": rasgos,
        "mgaleriainmueble": [img["id"] for img in d.get("images") or [] if img.get("id")],
        "mnombrecomunbarrio": d.get("commonNeighborhood"), "mbarrio": d.get("neighborhood"),
    }


def registro_ciencuadras(url: str) -> tuple[dict, dict] | None:
    pagina = externos.pagina_ciencuadras(url)
    if not pagina:
        return None
    g, mapa = pagina.get("generalData") or {}, (pagina.get("galleryData") or {}).get("map") or {}
    fotos = (pagina.get("galleryData") or {}).get("flatPhotos") or []
    rec = {
        "id": str(g.get("propertyId")), "url": url.replace("https://www.ciencuadras.com", ""),
        "rentPrice": g.get("leaseFee"), "adminValue": g.get("adminValue"),
        "privateArea": g.get("privateArea"), "area": g.get("builtArea"),
        "rooms": g.get("bedRoomNum"), "baths": g.get("bathRoomNum"), "garages": g.get("parkingNum"),
        "stratum": g.get("stratum"), "createdAt": g.get("publicationDate"),
        "coordinates": {"latitude": float(mapa["lat"]), "longitude": float(mapa["lon"])} if mapa.get("lat") else {},
        "neighborhood": g.get("neighborhoodName"), "locality": g.get("localityName"),
        "image": fotos[0]["url"] if fotos else None,
    }
    return rec, pagina


def leer(conn, guardar, encolar, dias: int = 7) -> int:
    """Lee los correos de los portales de los últimos `dias` y mete al flujo los anuncios que el
    sistema no conocía. Devuelve cuántos anuncios nuevos encontró."""
    imap = imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com"))
    imap.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
    nuevos = 0
    try:
        imap.select(os.environ.get("IMAP_CARPETA", "INBOX"), readonly=True)
        desde = (datetime.now() - timedelta(days=dias)).strftime("%d-%b-%Y")
        for portal, dominio in REMITENTES.items():
            _, datos = imap.search(None, "SINCE", desde, "FROM", dominio)
            for num in (datos[0] or b"").split():
                _, crudo = imap.fetch(num, "(BODY.PEEK[])")  # PEEK: no lo marca como leído
                msg = email.message_from_bytes(crudo[0][1])
                ident = msg.get("Message-ID") or f"{portal}:{num.decode()}"
                if conn.execute("SELECT 1 FROM correo WHERE mensaje = %s", (ident,)).fetchone():
                    continue
                enlaces = enlaces_de_anuncios(_texto(msg), portal)
                for url in enlaces:
                    if conn.execute("SELECT 1 FROM inmueble WHERE url = %s", (url,)).fetchone():
                        continue
                    try:
                        nuevos += _importar(conn, portal, url, guardar, encolar)
                    except Exception as e:
                        log.warning("No se pudo leer %s: %s", url, e)
                    time.sleep(3)
                try:
                    recibido = parsedate_to_datetime(msg.get("Date"))
                except (TypeError, ValueError):
                    recibido = None
                conn.execute("INSERT INTO correo (mensaje, portal, recibido, enlaces) VALUES (%s, %s, %s, %s)"
                             " ON CONFLICT (mensaje) DO NOTHING", (ident, portal, recibido, len(enlaces)))
    finally:
        imap.logout()
    return nuevos


def _importar(conn, portal: str, url: str, guardar, encolar) -> int:
    from .app import LECTORES

    lector, _ = LECTORES[portal]
    if portal == "fincaraiz":
        rec = registro_fincaraiz(url)
    elif portal == "metrocuadrado":
        rec = registro_metrocuadrado(url)
    else:
        par = registro_ciencuadras(url)
        rec = par[0] if par else None
    if not rec:
        return 0
    f = lector(rec)
    if portal == "ciencuadras":
        d = externos.detalle_ciencuadras(url, pagina=par[1])
        f.update(descripcion=d["descripcion"], imagenes=d["imagenes"] or f["imagenes"],
                 facilidades=sorted(set(f["facilidades"] + d["facilidades"])))
    guardar(conn, f)
    if portal == "ciencuadras":
        conn.execute("UPDATE inmueble SET externos = coalesce(externos, '{}'::jsonb) || '{\"detalle\": true}' WHERE id = %s",
                     (f["id"],))
    encolar(f["id"])
    return 1
