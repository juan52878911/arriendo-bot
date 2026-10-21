"""Scorer: recibe los inmuebles que captura Fredy, los cruza con las capas, los puntúa y avisa."""

import json
import logging
import os
import queue
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
import yaml

from . import comparar, db, externos, ingesta, mercado, rotacion
from .externos import e
from .puntaje import evaluar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("scorer")

TOKEN = os.environ.get("ARRIENDO_TOKEN", "")
WEB_URL = os.environ.get("ARRIENDO_WEB_URL", "")
BASE_FINCARAIZ = "https://www.fincaraiz.com.co"
BASE_METROCUADRADO = "https://www.metrocuadrado.com"
BASE_CIENCUADRAS = "https://www.ciencuadras.com"
BOGOTA_BBOX = (-74.45, 4.45, -73.95, 4.85)

pendientes: "queue.Queue[str]" = queue.Queue()


@asynccontextmanager
async def ciclo(_: FastAPI):
    db.migrate()
    threading.Thread(target=trabajador, daemon=True).start()
    threading.Thread(target=ingesta_periodica, daemon=True).start()
    threading.Thread(target=vigilante, daemon=True).start()
    threading.Thread(target=correo_periodico, daemon=True).start()
    yield


app = FastAPI(title="arriendo-bot", lifespan=ciclo)
# Fotos guardadas en el NAS (una por huella), para la página de etiquetado.
app.mount("/fotos", StaticFiles(directory=os.environ.get("ARRIENDO_FOTOS", "/fotos"), check_dir=False), name="fotos")


def autorizado(authorization: str = Header(default="")) -> None:
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401)


# Captura -----------------------------------------------------------------------------------

def _ficha(rec: dict, campo: str) -> int | None:
    for item in rec.get("technicalSheet") or []:
        if item.get("field") == campo:
            digitos = "".join(c for c in str(item.get("value") or "") if c.isdigit())
            return int(digitos) if digitos else None
    return None


def desde_fincaraiz(rec: dict) -> dict:
    precio = rec.get("price") or {}
    canon = precio.get("amount")
    total = precio.get("admin_included") or canon
    admin = _ficha(rec, "commonExpenses") or (total - canon if total and canon and total > canon else None)
    lat, lon = rec.get("latitude"), rec.get("longitude")
    dentro = isinstance(lat, (int, float)) and isinstance(lon, (int, float)) \
        and BOGOTA_BBOX[0] < lon < BOGOTA_BBOX[2] and BOGOTA_BBOX[1] < lat < BOGOTA_BBOX[3]
    return {
        "id": f"fincaraiz:{rec['id']}", "portal": "fincaraiz",
        "url": BASE_FINCARAIZ + rec.get("link", ""), "titulo": rec.get("title"),
        "descripcion": rec.get("description"), "canon": canon, "total": total, "administracion": admin,
        "area": rec.get("m2apto") or rec.get("m2Built") or rec.get("m2"),
        "habitaciones": _ficha(rec, "bedrooms"), "banos": _ficha(rec, "bathrooms"),
        "estrato": _ficha(rec, "stratum"), "piso": _ficha(rec, "floor"), "parqueaderos": _ficha(rec, "garage"),
        "facilidades": [f.get("name") for f in rec.get("facilities") or [] if f.get("name")],
        "imagenes": [i.get("image") for i in rec.get("images") or [] if i.get("image")][:20],
        "publicado": rec.get("created_at"),
        "lon": lon if dentro else None, "lat": lat if dentro else None,
        "barrio_portal": ((rec.get("locations") or {}).get("location_main") or {}).get("name"),
        "crudo": json.dumps(rec, ensure_ascii=False),
    }


def _num(valor) -> float | None:
    try:
        n = float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _punto(lat, lon) -> tuple:
    dentro = isinstance(lat, (int, float)) and isinstance(lon, (int, float)) \
        and BOGOTA_BBOX[0] < lon < BOGOTA_BBOX[2] and BOGOTA_BBOX[1] < lat < BOGOTA_BBOX[3]
    return (lat, lon) if dentro else (None, None)


def desde_metrocuadrado(rec: dict) -> dict:
    canon = _num(rec.get("mvalorarriendo"))
    admin = _num((rec.get("data") or {}).get("mvaloradministracion"))
    # "ascensor:S" → "ascensor"; "tipoEstufa:Gas" → "tipoEstufa Gas" (así "gas" cuenta para la cocina).
    rasgos = []
    for item in rec.get("featured") or []:
        clave, _, valor = str(item).strip().partition(":")
        if valor == "S":
            rasgos.append(clave)
        elif valor and valor not in ("N", "No tiene", "Otro") and not clave.startswith("nro") and not clave.startswith("area"):
            rasgos.append(f"{clave} {valor}")
    lat, lon = _punto(*((rec.get("localizacion") or {}).get(k) for k in ("lat", "lon")))
    ident = rec["midinmueble"]
    return {
        "id": f"metrocuadrado:{ident}", "portal": "metrocuadrado",
        "url": BASE_METROCUADRADO + rec.get("link", ""), "titulo": rec.get("title"),
        "descripcion": rec.get("comment"), "canon": canon, "total": canon + (admin or 0) if canon else None,
        "administracion": admin,
        "area": _num(rec.get("areaprivada")) or _num(rec.get("marea")) or _num(rec.get("mareac")),
        "habitaciones": int(_num(rec.get("mnrocuartos")) or 0) or None,
        "banos": int(_num(rec.get("mnrobanos")) or 0) or None,
        "estrato": rec.get("estrato"), "piso": None,
        "parqueaderos": int(_num(rec.get("mnrogarajes")) or 0),
        "facilidades": rasgos,
        "imagenes": [f"https://multimedia.metrocuadrado.com/{ident}/{g}_p.jpg"
                     for g in rec.get("mgaleriainmueble") or []][:20],
        "publicado": None, "lat": lat, "lon": lon,
        "barrio_portal": rec.get("mnombrecomunbarrio") or rec.get("mbarrio"),
        "crudo": json.dumps(rec, ensure_ascii=False),
    }


def desde_ciencuadras(rec: dict) -> dict:
    canon = _num(rec.get("rentPrice"))
    admin = _num(rec.get("adminValue"))
    coords = rec.get("coordinates") or {}
    lat, lon = _punto(coords.get("latitude"), coords.get("longitude"))
    rasgos = [nombre for campo, nombre in (("gym", "gimnasio"), ("communalPool", "piscina comunal"),
                                            ("ascended", "ascensor")) if rec.get(campo) in (True, 1, "1")]
    return {
        "id": f"ciencuadras:{rec['id']}", "portal": "ciencuadras",
        "url": BASE_CIENCUADRAS + rec.get("url", ""),
        "titulo": f"{rec.get('realEstateType') or 'Inmueble'} en arriendo en {rec.get('neighborhood') or rec.get('locality') or 'Bogotá'}",
        "descripcion": None, "canon": canon, "total": canon + (admin or 0) if canon else None,
        "administracion": admin, "area": _num(rec.get("privateArea")) or _num(rec.get("area")),
        "habitaciones": int(_num(rec.get("rooms")) or 0) or None,
        "banos": int(_num(rec.get("baths")) or 0) or None,
        "estrato": int(_num(rec.get("stratum")) or 0) or None, "piso": None,
        "parqueaderos": int(_num(rec.get("garages")) or 0),
        "facilidades": rasgos, "imagenes": [externos.foto_ciencuadras(rec["image"])] if rec.get("image") else [],
        "publicado": (rec.get("createdAt") or "")[:10] or None, "lat": lat, "lon": lon,
        "barrio_portal": rec.get("neighborhood"), "crudo": json.dumps(rec, ensure_ascii=False),
    }


LECTORES = {
    "fincaraiz": (desde_fincaraiz, "id"),
    "metrocuadrado": (desde_metrocuadrado, "midinmueble"),
    "ciencuadras": (desde_ciencuadras, "id"),
}


def guardar(conn, f: dict) -> bool:
    """Inserta o actualiza. Devuelve True si hay que (re)puntuar: es nuevo o cambió el precio."""
    previo = conn.execute("SELECT total FROM inmueble WHERE id = %s", (f["id"],)).fetchone()
    conn.execute(
        """INSERT INTO inmueble (id, portal, url, titulo, descripcion, canon, total, administracion, area,
             habitaciones, banos, estrato, piso, parqueaderos, facilidades, imagenes, publicado, geom,
             barrio_portal, crudo)
           VALUES (%(id)s, %(portal)s, %(url)s, %(titulo)s, %(descripcion)s, %(canon)s, %(total)s,
             %(administracion)s, %(area)s, %(habitaciones)s, %(banos)s, %(estrato)s, %(piso)s,
             %(parqueaderos)s, %(facilidades)s, %(imagenes)s, %(publicado)s,
             CASE WHEN %(lon)s::float IS NULL THEN NULL
                  ELSE ST_SetSRID(ST_MakePoint(%(lon)s::float, %(lat)s::float), 4326) END,
             %(barrio_portal)s, %(crudo)s)
           ON CONFLICT (id) DO UPDATE SET url = EXCLUDED.url, titulo = EXCLUDED.titulo,
             canon = EXCLUDED.canon, total = EXCLUDED.total,
             administracion = EXCLUDED.administracion, area = EXCLUDED.area,
             habitaciones = EXCLUDED.habitaciones, banos = EXCLUDED.banos, estrato = EXCLUDED.estrato,
             piso = EXCLUDED.piso, parqueaderos = EXCLUDED.parqueaderos, facilidades = EXCLUDED.facilidades,
             imagenes = CASE WHEN cardinality(inmueble.imagenes) > cardinality(EXCLUDED.imagenes)
                             THEN inmueble.imagenes ELSE EXCLUDED.imagenes END,
             descripcion = coalesce(EXCLUDED.descripcion, inmueble.descripcion),
             geom = coalesce(EXCLUDED.geom, inmueble.geom),
             crudo = EXCLUDED.crudo, visto_ultimo = now(), retirado = NULL""", f)
    if f["total"] and (previo is None or previo["total"] != f["total"]):
        conn.execute("INSERT INTO precio (inmueble, total) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (f["id"], f["total"]))
        return True
    return False


@app.post("/raw", dependencies=[Depends(autorizado)])
def raw(cuerpo: dict) -> dict:
    if cuerpo.get("portal") not in LECTORES:
        raise HTTPException(status_code=400, detail="portal no soportado")
    lector, clave = LECTORES[cuerpo["portal"]]
    nuevos = 0
    with db.connect() as conn:
        for rec in cuerpo.get("records") or []:
            if rec.get(clave) is None:
                continue
            f = lector(rec)
            if guardar(conn, f):
                pendientes.put(f["id"])
                nuevos += 1
    return {"recibidos": len(cuerpo.get("records") or []), "a_puntuar": nuevos}


@app.post("/webhook", dependencies=[Depends(autorizado)])
def webhook(cuerpo: dict) -> dict:
    """Aviso HTTP de Fredy: solo se guarda el enlace a la ficha en Fredy."""
    with db.connect() as conn:
        for l in cuerpo.get("listings") or []:
            if l.get("url") and l.get("fredyUrl"):
                conn.execute("UPDATE inmueble SET fredy_url = %s WHERE url = %s", (l["fredyUrl"], l["url"]))
    return {"ok": True}


# Puntaje -----------------------------------------------------------------------------------

def contexto(conn, i: dict, cfg: dict) -> dict:
    """Lo que dicen las capas del punto donde está el inmueble."""
    ctx: dict = {}
    if i["geom"] is None:
        return ctx
    s = conn.execute("""SELECT codigo, nombre, localidad, seguridad_pct, periodo, estrato_moda, incidentes, area_km2 FROM sector
                        WHERE ST_Contains(geom, %s::geometry) LIMIT 1""", (i["geom"],)).fetchone()
    if s:
        ctx.update(sector=s["codigo"], sector_nombre=s["nombre"], localidad=s["localidad"],
                   seguridad_pct=s["seguridad_pct"], periodo=s["periodo"], estrato_moda=s["estrato_moda"],
                   **comparar.por_km2(s["incidentes"], s["area_km2"]),
                   estabilidad=comparar.estabilidad_de(conn, [s["codigo"]]).get(s["codigo"]))
    else:
        l = conn.execute("SELECT nombre FROM localidad WHERE ST_Contains(geom, %s::geometry) LIMIT 1",
                         (i["geom"],)).fetchone()
        ctx["localidad"] = l["nombre"] if l else None
    t = conn.execute("""SELECT nombre, ST_Distance(geom::geography, %(g)s::geometry::geography) AS m
                        FROM estacion_tm ORDER BY geom <-> %(g)s::geometry LIMIT 1""", {"g": i["geom"]}).fetchone()
    if t:
        ctx.update(tm_m=t["m"], tm_nombre=t["nombre"])
    v = conn.execute("""SELECT nombre, tipo, ST_Distance(geom::geography, %(g)s::geometry::geography) AS m
                        FROM via ORDER BY geom <-> %(g)s::geometry LIMIT 1""", {"g": i["geom"]}).fetchone()
    if v:
        ctx.update(via_m=v["m"], via_nombre=v["nombre"] or v["tipo"])
    a = conn.execute("SELECT pm25, ano FROM aire WHERE ST_Contains(geom, %s::geometry) LIMIT 1", (i["geom"],)).fetchone()
    if a:
        ctx.update(pm25=a["pm25"], pm25_ano=a["ano"])
    pq = conn.execute("""SELECT nombre, area_m2, ST_Distance(geom::geography, %(g)s::geometry::geography) AS m
                         FROM parque WHERE area_m2 >= %(min)s ORDER BY geom <-> %(g)s::geometry LIMIT 1""",
                      {"g": i["geom"], "min": cfg["parametros"]["parque_min_m2"]}).fetchone()
    if pq:
        ctx.update(parque_m=pq["m"], parque_nombre=pq["nombre"] or "parque sin nombre")
    ctx["mercados"] = conn.execute("""SELECT count(*) AS n FROM poi
                                      WHERE ST_DWithin(geom::geography, %s::geometry::geography, %s)""",
                                   (i["geom"], cfg["parametros"]["mercado_radio_m"])).fetchone()["n"]
    ctx["ciclorruta_m"] = (conn.execute("""SELECT ST_Distance(geom::geography, %(g)s::geometry::geography) AS m
                                           FROM ciclorruta ORDER BY geom <-> %(g)s::geometry LIMIT 1""",
                                        {"g": i["geom"]}).fetchone() or {}).get("m")
    minimo = cfg["parametros"]["muestra_min_precio"]
    if not i["area"]:
        return ctx
    for campo, valor in (("sector", ctx.get("sector")), ("localidad", ctx.get("localidad"))):
        if not valor:
            continue
        # Solo inmuebles de tamaño parecido: un apartaestudio cuesta mucho más por m² que un
        # apartamento familiar, y mezclarlos hace parecer sospechoso cualquier precio normal.
        m = conn.execute(f"""SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY total / area) AS med, count(*) AS n
                             FROM inmueble WHERE {campo} = %s AND id <> %s AND total > 0
                               AND area BETWEEN %s * 0.6 AND %s * 1.6""",
                         (valor, i["id"], i["area"], i["area"])).fetchone()
        if m and m["n"] >= minimo:
            ctx.update(mediana_m2=m["med"], mediana_n=m["n"])
            break
    return ctx


def duplicado(conn, i: dict) -> str | None:
    """El mismo apartamento publicado en otro portal: a menos de 80 m, mismas habitaciones, área y
    precio a menos de un 10 %. Devuelve el que se vio primero."""
    if i["geom"] is None or not i["habitaciones"] or not i["area"] or not i["total"]:
        return None
    otro = conn.execute("""
        SELECT id FROM inmueble
        WHERE portal <> %(portal)s AND habitaciones = %(hab)s
          AND abs(area - %(area)s) <= 0.1 * %(area)s AND abs(total - %(total)s) <= 0.1 * %(total)s
          AND ST_DWithin(geom::geography, %(geom)s::geometry::geography, 80)
          AND visto_primero <= %(visto)s
        ORDER BY visto_primero LIMIT 1""",
        {"portal": i["portal"], "hab": i["habitaciones"], "area": i["area"], "total": i["total"],
         "geom": i["geom"], "visto": i["visto_primero"]}).fetchone()
    return otro["id"] if otro else None


def resumen_fotos(conn, id_: str) -> dict | None:
    """Lo que dicen las fotos de un inmueble, agregado por criterio. Cada foto real cuenta una vez
    aunque el portal la haya subido repetida."""
    fotos = conn.execute("""SELECT DISTINCT im.huella, im.espacio, im.etiquetas FROM foto_url f
                            JOIN imagen im ON im.huella = f.huella WHERE f.inmueble = %s""", (id_,)).fetchall()
    if not fotos:
        return None

    def p(cabeza: str, clase: str, espacios: tuple | None = None, agg=max) -> float | None:
        v = [f["etiquetas"][cabeza][clase] for f in fotos
             if cabeza in f["etiquetas"] and (espacios is None or f["espacio"] in espacios)]
        return agg(v) if v else None

    media = lambda v: sum(v) / len(v)
    piso = [f["etiquetas"]["piso"] for f in fotos if "piso" in f["etiquetas"]]
    return {
        "n": len(fotos),
        "espacios": sorted({f["espacio"] for f in fotos}),
        "horno": p("horno", "si"), "gas": p("estufa", "gas"), "extractor": p("extractor", "si"),
        "piso_bueno": media([x.get("porcelanato", 0) + x.get("laminado", 0) for x in piso]) if piso else None,
        "ducha_division": p("ducha", "division"),
        "luz": p("luz", "alta", agg=media),
        "render": p("render", "render"),
    }


# Dos fotos son la misma si sus huellas difieren en pocos bits y su contenido es casi idéntico:
# la huella sola puede coincidir en fotos distintas pero muy simples (una pared blanca).
MISMA_FOTO_BITS = 6
MISMA_FOTO_COS = 0.95


def registrar_foto(conn, f: dict, inmueble: str) -> tuple[str, str | None]:
    """Guarda una foto analizada sin duplicar. Devuelve (huella canónica, archivo que sobra en el NAS)."""
    gemela = conn.execute("""
        SELECT huella, archivo, ancho * alto AS area FROM imagen
        WHERE bit_count(('x' || huella)::bit(64) # ('x' || %(h)s)::bit(64)) <= %(bits)s
          AND (SELECT sum(x * y) FROM unnest(emb, %(emb)s::real[]) AS t(x, y)) >= %(cos)s
        ORDER BY bit_count(('x' || huella)::bit(64) # ('x' || %(h)s)::bit(64)) LIMIT 1""",
        {"h": f["huella"], "emb": f["emb"], "bits": MISMA_FOTO_BITS, "cos": MISMA_FOTO_COS}).fetchone()
    sobra = None
    if gemela is None:
        conn.execute("""INSERT INTO imagen (huella, emb, espacio, etiquetas, ancho, alto, archivo)
                        VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (huella) DO NOTHING""",
                     (f["huella"], f["emb"], f["espacio"], json.dumps(f["etiquetas"]), f["ancho"], f["alto"], f["archivo"]))
        canonica = f["huella"]
    else:
        canonica = gemela["huella"]
        if f["archivo"] != gemela["archivo"]:
            if f["ancho"] * f["alto"] > (gemela["area"] or 0):
                # Llegó una copia más grande: se queda su archivo y sobra el anterior.
                conn.execute("UPDATE imagen SET archivo = %s, ancho = %s, alto = %s WHERE huella = %s",
                             (f["archivo"], f["ancho"], f["alto"], canonica))
                sobra = gemela["archivo"]
            else:
                sobra = f["archivo"]
    conn.execute("""INSERT INTO foto_url (url, huella, inmueble) VALUES (%s, %s, %s)
                    ON CONFLICT (url) DO UPDATE SET huella = EXCLUDED.huella, visto = now()""",
                 (f["url"], canonica, inmueble))
    return canonica, sobra


def analizar_fotos(conn, i: dict) -> None:
    """Analiza solo las URLs que aún no se conocen; las fotos repetidas se funden en una."""
    conocidas = {r["url"] for r in conn.execute("SELECT url FROM foto_url WHERE inmueble = %s", (i["id"],))}
    urls = [u for u in (i["imagenes"] or []) if u and u not in conocidas]
    if not urls:
        return
    r = externos.vision("/analizar", {"urls": urls})
    sobrantes = []
    for f in (r or {}).get("fotos", []):
        _, sobra = registrar_foto(conn, f, i["id"])
        if sobra:
            sobrantes.append(sobra)
    if sobrantes:
        externos.vision("/borrar", {"archivos": sobrantes})


def puntuar(id_: str) -> None:
    cfg = db.config()
    with db.connect() as conn:
        i = conn.execute("SELECT *, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM inmueble WHERE id = %s",
                         (id_,)).fetchone()
        if i is None:
            return
        ctx = contexto(conn, i, cfg)
        i["duplicado_de"] = duplicado(conn, i)
        r = evaluar(i, ctx, cfg)
        # Consultas externas solo para los que sobreviven a los filtros baratos, una vez por inmueble.
        if not r["descartes"] and i["lat"] is not None:
            if i["tiempo_min"] is None:
                minutos = externos.minutos_a_destino(i["lat"], i["lon"])
                if minutos is not None:
                    i["tiempo_min"], i["tiempo_fuente"] = minutos, "transitous"
                time.sleep(1)
            ext = i["externos"] or {}
            if i["portal"] == "ciencuadras" and "detalle" not in ext:
                d = externos.detalle_ciencuadras(i["url"])
                if d:
                    facilidades = sorted(set((i["facilidades"] or []) + d["facilidades"]))
                    conn.execute("""UPDATE inmueble SET descripcion = coalesce(%s, descripcion), imagenes = %s,
                                      facilidades = %s WHERE id = %s""",
                                 (d["descripcion"], d["imagenes"] or i["imagenes"], facilidades, id_))
                    i["descripcion"], i["imagenes"] = d["descripcion"] or i["descripcion"], d["imagenes"] or i["imagenes"]
                    i["facilidades"] = facilidades
                ext = {**ext, "detalle": bool(d)}
                conn.execute("UPDATE inmueble SET externos = %s WHERE id = %s", (json.dumps(ext), id_))
                i["externos"] = ext
            if "suelo_m2" not in ext:
                ext = {**ext, "suelo_m2": externos.valor_suelo_m2(i["lat"], i["lon"]),
                       "siniestros_150m": externos.siniestros_cerca(i["lat"], i["lon"])}
            if "ruido_noche_db" not in ext:
                ext = {**ext, "ruido_noche_db": externos.ruido_noche_db(i["lat"], i["lon"])}
                conn.execute("UPDATE inmueble SET externos = %s WHERE id = %s", (json.dumps(ext), id_))
                i["externos"] = ext
            ctx.update(ext)
            r = evaluar(i, ctx, cfg)
            # Fotos solo para quien ya está cerca del umbral: son la consulta más cara.
            if r["puntaje"] >= cfg["avisos"]["umbral"] - cfg["avisos"].get("margen_fotos", 15):
                analizar_fotos(conn, i)
        ctx["fotos"] = resumen_fotos(conn, id_)
        ctx["sospechas"] = conn.execute("""SELECT s.parecido, s.distancia, s.motivo, i.url, i.retirado IS NOT NULL AS retirado
                                           FROM sospecha s JOIN inmueble i ON i.id = s.parecido
                                           WHERE s.inmueble = %s""", (id_,)).fetchall()
        r = evaluar(i, ctx, cfg)
        comp = comparar.comparar(i, ctx, r, comparar.referencia(conn), comparar.privado())
        if comp:
            conn.execute("UPDATE inmueble SET comparacion = %s WHERE id = %s", (json.dumps(comp), id_))
            if comp["vale_la_pena"]:
                r["alertas"].insert(0, f"Le gana a tu apartamento actual en {len(comp['gana_en'])} de "
                                       f"{comp['total_dimensiones']}: {', '.join(comp['gana_en'])}. Tu parte: "
                                       f"${comp['pago_tuyo']:,.0f}, nivel {comp['nivel']} ({comp['pct_salario']} % de tu neto).".replace(",", "."))
        conn.execute("""UPDATE inmueble SET sector = %s, localidad = %s, tm_m = %s, tiempo_min = %s,
                          tiempo_fuente = %s, puntaje = %s, detalle = %s, descartes = %s, alertas = %s,
                          duplicado_de = %s
                        WHERE id = %s""",
                     (ctx.get("sector"), ctx.get("localidad"), ctx.get("tm_m"), i["tiempo_min"],
                      i["tiempo_fuente"], r["puntaje"], json.dumps(r["detalle"], ensure_ascii=False),
                      r["descartes"], r["alertas"], i["duplicado_de"], id_))
        avisar(conn, i, ctx, r, cfg)


def avisar(conn, i: dict, ctx: dict, r: dict, cfg: dict) -> None:
    if r["descartes"] or r["puntaje"] < cfg["avisos"]["umbral"]:
        return
    if i.get("duplicado_de"):
        # Ya se avisó (o se avisará) por el portal donde apareció primero.
        return
    if i["avisado"] is not None:
        previos = conn.execute("SELECT total FROM precio WHERE inmueble = %s ORDER BY visto DESC LIMIT 2",
                               (i["id"],)).fetchall()
        bajo = len(previos) == 2 and previos[0]["total"] < previos[1]["total"]
        if not (bajo and cfg["avisos"].get("avisar_bajadas_de_precio")):
            return
        encabezado = f"⬇️ Bajó a ${previos[0]['total']:,.0f} (antes ${previos[1]['total']:,.0f})".replace(",", ".")
    else:
        encabezado = "🏠 Nuevo"
    zona = ctx.get("sector_nombre") or i["barrio_portal"] or ""
    datos = " · ".join(x for x in [
        f"${i['total']:,.0f} total".replace(",", ".") if i["total"] else None,
        f"{i['area']:.0f} m²" if i["area"] else None,
        f"{i['habitaciones']} hab / {i['banos']} baños" if i["habitaciones"] else None,
        f"estrato {i['estrato']}" if i["estrato"] else None,
        f"{i['tiempo_min']} min a {cfg['destino']['nombre']}" if i["tiempo_min"] else None] if x)
    mejores = sorted(r["detalle"].items(), key=lambda kv: -kv[1]["valor"] * kv[1]["peso"])[:4]
    lineas = [f"{encabezado} · <b>{r['puntaje']:.0f}/100 · {e(zona.title())}</b>", e(datos), ""]
    lineas += [f"• {e(k)}: {e(v['nota'])}" for k, v in mejores]
    lineas += [f"⚠️ {e(a)}" for a in r["alertas"]]
    enlaces = [f'<a href="{e(i["url"])}">Ver anuncio</a>']
    if WEB_URL:
        enlaces.append(f'<a href="{e(WEB_URL)}/#{e(i["id"])}">Mapa y ranking</a>')
    lineas += ["", " · ".join(enlaces)]
    if externos.telegram("\n".join(lineas), foto=(i["imagenes"] or [None])[0]):
        conn.execute("UPDATE inmueble SET avisado = now() WHERE id = %s", (i["id"],))


def trabajador() -> None:
    while True:
        id_ = pendientes.get()
        try:
            puntuar(id_)
        except Exception:
            log.exception("No se pudo puntuar %s", id_)


# Vigilancia: que nada falle en silencio ------------------------------------------------------

PORTALES = {"fincaraiz": "Fincaraíz", "metrocuadrado": "Metrocuadrado", "ciencuadras": "Ciencuadras"}


def problemas(conn) -> list[tuple[str, str]]:
    """(clave, mensaje) de cada cosa que hoy no está funcionando."""
    fuera = []
    for portal, nombre in PORTALES.items():
        r = conn.execute("SELECT max(visto_ultimo) AS u, now() - max(visto_ultimo) AS hace FROM inmueble WHERE portal = %s",
                         (portal,)).fetchone()
        if r["u"] is None or r["hace"].total_seconds() > 24 * 3600:
            horas = "nunca" if r["u"] is None else f"hace {r['hace'].total_seconds() / 3600:.0f} h"
            fuera.append((f"portal:{portal}", f"{nombre} no trae anuncios desde {horas}: puede haber cambiado su "
                                              "página o estar bloqueando. Revisa el log de Fredy."))
    for f in db.fuentes():
        r = conn.execute("SELECT now() - actualizada AS hace FROM fuente WHERE id = %s", (f["id"],)).fetchone()
        if r is None or r["hace"].days > 3 * f["frecuencia_dias"]:
            fuera.append((f"fuente:{f['id']}", f"La fuente de datos «{f['id']}» no se actualiza desde hace "
                                               f"{'siempre' if r is None else str(r['hace'].days) + ' días'}."))
    if externos.VISION_URL and externos.vision("/clasificar", {"fotos": []}, timeout=20) is None:
        fuera.append(("vision", "El servicio de visión no responde: las fotos no se están analizando."))
    return fuera


def sigue_publicado(id_: str, url: str) -> bool | None:
    """Abre el anuncio y busca la señal de retiro propia de cada portal (None: no se pudo saber).
    - Fincaraíz: un anuncio borrado redirige al listado con `addeletedid=`; uno desactivado sigue
      respondiendo pero trae `inactiveSimilarInfo` (le ofrece similares al visitante).
    - Metrocuadrado: responde 200 con la página «Error 404 - Detalle».
    - Ciencuadras: la página de un anuncio vivo trae su propio `"propertyId":<id>`."""
    portal, resto = id_.split(":", 1)
    ident = resto.split("-")[-1]
    try:
        r = externos.HTTP.get(url, headers=externos.NAVEGADOR, follow_redirects=True)
    except Exception as e:
        log.warning("No se pudo comprobar %s: %s", url, e)
        return None
    if r.status_code in (404, 410):
        return False
    if r.status_code != 200:
        return None
    final, t = str(r.url), r.text
    if portal == "fincaraiz":
        return "addeletedid" not in final and '"inactiveSimilarInfo":{' not in t and ident in final
    if portal == "metrocuadrado":
        return "<title>Error 404" not in t and ident in final
    if portal == "ciencuadras":
        return f'propertyId&q;:{ident}' in t or f'"propertyId":{ident}' in t
    return ident in final and ident in t


def comprobar_retirados(conn, limite: int = 40) -> int:
    """Abre anuncios para saber si siguen publicados, por prioridad: primero lo que te recomiendo hoy
    (cada 12 h), luego lo que dejó de verse en los listados (bajar de página no es retirarse) y por
    último cualquier activo que no se haya comprobado en 5 días."""
    filas = conn.execute("""
        SELECT id, url FROM (
          SELECT i.id, i.url, i.comprobado,
                 CASE WHEN EXISTS (SELECT 1 FROM recomendacion r WHERE r.inmueble = i.id AND r.dia = current_date)
                           AND (i.comprobado IS NULL OR i.comprobado < now() - interval '12 hours') THEN 0
                      WHEN i.visto_ultimo < now() - interval '1 day'
                           AND (i.comprobado IS NULL OR i.comprobado < now() - interval '2 days') THEN 1
                      WHEN i.comprobado IS NULL OR i.comprobado < now() - interval '5 days' THEN 2 END AS prioridad
          FROM inmueble i WHERE i.retirado IS NULL AND i.duplicado_de IS NULL) q
        WHERE prioridad IS NOT NULL ORDER BY prioridad, comprobado NULLS FIRST LIMIT %s""", (limite,)).fetchall()
    retirados = 0
    for f in filas:
        vivo = sigue_publicado(f["id"], f["url"])
        if vivo is not None:
            conn.execute(f"UPDATE inmueble SET {'comprobado' if vivo else 'retirado'} = now() WHERE id = %s", (f["id"],))
            retirados += not vivo
        time.sleep(3)
    if retirados:
        mercado.registrar_recomendados(conn)   # que la lista de hoy no siga mostrando lo retirado
    return retirados


@app.post("/api/comprobar", dependencies=[Depends(autorizado)])
def comprobar(limite: int = 40) -> dict:
    with db.connect() as conn:
        return {"retirados": comprobar_retirados(conn, limite)}


def purgar_fotos(conn, dias: int = 180) -> int:
    """Borra del NAS las fotos que ya no sirven: sin etiqueta y cuyos anuncios están todos retirados
    hace más de `dias` días (o ya no existen). Las etiquetadas se quedan: son el material de entrenamiento."""
    filas = conn.execute("""
        DELETE FROM imagen im
        WHERE NOT EXISTS (SELECT 1 FROM etiqueta_img e WHERE e.huella = im.huella)
          AND NOT EXISTS (SELECT 1 FROM foto_url fu JOIN inmueble i ON i.id = fu.inmueble
                          WHERE fu.huella = im.huella
                            AND (i.retirado IS NULL OR i.retirado > now() - make_interval(days => %s)))
        RETURNING archivo""", (dias,)).fetchall()
    archivos = [f["archivo"] for f in filas if f["archivo"]]
    if archivos:
        externos.vision("/borrar", {"archivos": archivos})
    return len(archivos)


def vigilante() -> None:
    while True:
        time.sleep(3600)
        try:
            with db.connect() as conn:
                n = comprobar_retirados(conn)
                if n:
                    log.info("%s anuncios comprobados como retirados", n)
                n = purgar_fotos(conn)
                if n:
                    log.info("%s fotos viejas borradas del NAS", n)
                for clave, mensaje in problemas(conn):
                    ya = conn.execute("SELECT 1 FROM aviso_salud WHERE clave = %s AND enviado > now() - interval '24 hours'",
                                      (clave,)).fetchone()
                    if ya:
                        continue
                    log.warning("Problema: %s", mensaje)
                    externos.telegram(f"⚠️ <b>arriendo-bot</b>: {e(mensaje)}")
                    conn.execute("""INSERT INTO aviso_salud (clave) VALUES (%s)
                                    ON CONFLICT (clave) DO UPDATE SET enviado = now()""", (clave,))
        except Exception:
            log.exception("La vigilancia falló; sigue en una hora")


@app.post("/api/aviso", dependencies=[Depends(autorizado)])
def aviso(cuerpo: dict) -> dict:
    """Aviso de salud desde fuera del scorer (p. ej. la prueba semanal de respaldos)."""
    mensaje = str(cuerpo.get("mensaje", ""))[:500]
    log.warning("Aviso externo: %s", mensaje)
    externos.telegram(f"⚠️ <b>arriendo-bot</b>: {e(mensaje)}")
    with db.connect() as conn:
        conn.execute("""INSERT INTO aviso_salud (clave) VALUES (%s)
                        ON CONFLICT (clave) DO UPDATE SET enviado = now()""", (f"externo:{cuerpo.get('clave', 'otro')}",))
    return {"ok": True}


@app.get("/api/estado")
def estado_sistema() -> dict:
    with db.connect() as conn:
        return {"problemas": [m for _, m in problemas(conn)],
                "portales": conn.execute("""SELECT portal, count(*) FILTER (WHERE retirado IS NULL) AS activos,
                                              count(retirado) AS retirados, max(visto_ultimo) AS ultimo
                                            FROM inmueble GROUP BY portal ORDER BY portal""").fetchall(),
                "fuentes": conn.execute("SELECT id, actualizada, filas FROM fuente ORDER BY id").fetchall()}


def ingesta_periodica() -> None:
    while True:
        try:
            ingesta.run()
        except Exception:
            log.exception("La ingesta falló; se reintenta en 6 h")
            time.sleep(6 * 3600)
            continue
        time.sleep(24 * 3600)


# Correos de alerta de los portales ------------------------------------------------------------

def correo_periodico() -> None:
    from . import correo
    if not correo.configurado():
        log.info("Lector de correos sin configurar (IMAP_USER / IMAP_PASSWORD); no se leen alertas")
        return
    while True:
        try:
            with db.connect() as conn:
                nuevos = correo.leer(conn, guardar, pendientes.put)
            if nuevos:
                log.info("Correos de alerta: %s anuncios nuevos", nuevos)
        except Exception:
            log.exception("La lectura de correos falló; se reintenta en 30 min")
        time.sleep(1800)


# Consulta ----------------------------------------------------------------------------------

@app.post("/api/recalcular", dependencies=[Depends(autorizado)])
def recalcular() -> dict:
    """Tras cambiar config.yaml: vuelve a puntuar todo sin volver a avisar lo ya avisado."""
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM inmueble")]
    for id_ in ids:
        pendientes.put(id_)
    return {"en_cola": len(ids)}


@app.get("/api/inmuebles")
def inmuebles(todos: bool = False, limite: int = 300) -> list[dict]:
    filtro = "" if todos else ("WHERE coalesce(cardinality(descartes), 0) = 0 AND puntaje IS NOT NULL"
                               " AND duplicado_de IS NULL AND retirado IS NULL")
    with db.connect() as conn:
        return conn.execute(f"""
            SELECT id, portal, duplicado_de, retirado, comparacion, url, fredy_url, titulo, total, area, habitaciones, banos, estrato, puntaje, detalle,
                   descartes, alertas, tiempo_min, tiempo_fuente, barrio_portal, localidad,
                   (SELECT nombre FROM sector s WHERE s.codigo = i.sector) AS sector_nombre,
                   imagenes[1] AS foto, publicado, visto_primero, ST_Y(geom) AS lat, ST_X(geom) AS lon
            FROM inmueble i {filtro} ORDER BY puntaje DESC NULLS LAST LIMIT %s""", (limite,)).fetchall()


@app.get("/api/sectores")
def sectores() -> dict:
    """Capa de seguridad para el mapa, simplificada para que pese poco."""
    with db.connect() as conn:
        filas = conn.execute("""SELECT codigo, nombre, localidad, seguridad_pct, estrato_moda,
                                  ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.0003), 5) AS g
                                FROM sector""").fetchall()
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": json.loads(f.pop("g")), "properties": f} for f in filas]}


CAPAS = {
    # nombre: (consulta, simplificación en grados)
    "isocrona": ("SELECT id, minutos, geom FROM isocrona", 0.0002),
    "aire": ("SELECT id, pm25, ano, geom FROM aire", 0.0005),
    "parques": ("SELECT id, nombre, round(area_m2 / 10000) AS ha, geom FROM parque WHERE area_m2 >= 20000", 0.0001),
    "mercados": ("SELECT id, tipo, nombre, geom FROM poi", 0),
    "vias": ("SELECT id, tipo, nombre, geom FROM via WHERE tipo IN ('motorway', 'trunk', 'primary')", 0.0002),
    "estaciones": ("SELECT id, nombre, geom FROM estacion_tm", 0),
}


@app.get("/api/capas/{nombre}")
def capa(nombre: str) -> dict:
    """Capas de contexto para el mapa, en GeoJSON simplificado."""
    if nombre not in CAPAS:
        raise HTTPException(status_code=404)
    consulta, tolerancia = CAPAS[nombre]
    with db.connect() as conn:
        filas = conn.execute(f"""SELECT q.*, ST_AsGeoJSON(CASE WHEN %s > 0 THEN ST_SimplifyPreserveTopology(geom, %s)
                                                             ELSE geom END, 5) AS g
                                 FROM ({consulta}) q""", (tolerancia, tolerancia)).fetchall()
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": json.loads(f.pop("g")), "properties": {k: v for k, v in f.items() if k != "geom"}}
        for f in filas]}


@app.get("/api/fuentes")
def fuentes() -> list[dict]:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM fuente ORDER BY id").fetchall()


# Etiquetado y entrenamiento de las cabezas de visión ---------------------------------------------

@app.get("/api/etiquetar/siguiente")
def siguiente(cabeza: str) -> dict:
    """La foto más dudosa para esa cabeza que todavía no tiene etiqueta. Se sirve desde el NAS, así
    que sigue disponible aunque el anuncio ya no exista."""
    cfg = yaml.safe_load((db.CONFIG_DIR / "vision.yaml").read_text())
    if cabeza not in cfg["cabezas"]:
        raise HTTPException(status_code=404)
    with db.connect() as conn:
        f = conn.execute("""
            SELECT im.huella, 'fotos/' || im.archivo AS url, im.espacio, im.etiquetas->%(c)s AS pred,
                   (SELECT inmueble FROM foto_url fu WHERE fu.huella = im.huella LIMIT 1) AS inmueble,
                   (SELECT count(*) FROM foto_url fu WHERE fu.huella = im.huella) AS copias
            FROM imagen im LEFT JOIN etiqueta_img e ON e.huella = im.huella AND e.cabeza = %(c)s
            WHERE e.huella IS NULL AND im.etiquetas ? %(c)s
            ORDER BY (SELECT max(v::float) FROM jsonb_each_text(im.etiquetas->%(c)s) AS t(k, v)) ASC
            LIMIT 1""", {"c": cabeza}).fetchone()
        hechas = conn.execute("SELECT count(*) AS n FROM etiqueta_img WHERE cabeza = %s", (cabeza,)).fetchone()["n"]
    return {"foto": f, "clases": cfg["cabezas"][cabeza]["clases"], "etiquetadas": hechas}


@app.post("/api/etiquetas")
def etiquetar(cuerpo: dict) -> dict:
    with db.connect() as conn:
        conn.execute("""INSERT INTO etiqueta_img (huella, cabeza, clase) VALUES (%(huella)s, %(cabeza)s, %(clase)s)
                        ON CONFLICT (huella, cabeza) DO UPDATE SET clase = EXCLUDED.clase, creada = now()""", cuerpo)
    return {"ok": True}


@app.post("/api/entrenar")
def entrenar() -> dict:
    """Entrena con las etiquetas, reclasifica todas las fotos guardadas y repuntúa."""
    with db.connect() as conn:
        filas = conn.execute("""SELECT e.cabeza, e.clase, im.emb FROM etiqueta_img e
                                JOIN imagen im ON im.huella = e.huella""").fetchall()
        ejemplos: dict = {}
        for r in filas:
            ejemplos.setdefault(r["cabeza"], []).append({"emb": r["emb"], "clase": r["clase"]})
        informe = externos.vision("/entrenar", {"ejemplos": ejemplos})
        if informe is None:
            raise HTTPException(status_code=503, detail="servicio de visión no disponible")
        reclasificar_imagenes(conn)
        ids = [r["inmueble"] for r in conn.execute("SELECT DISTINCT inmueble FROM foto_url")]
    for id_ in ids:
        pendientes.put(id_)
    return {"cabezas": informe, "repuntuando": len(ids)}


def reclasificar_imagenes(conn) -> None:
    fotos = conn.execute("SELECT huella AS url, emb FROM imagen WHERE emb IS NOT NULL").fetchall()
    for lote in range(0, len(fotos), 200):
        r = externos.vision("/clasificar", {"fotos": fotos[lote:lote + 200]}) or {"fotos": []}
        for f in r["fotos"]:
            conn.execute("UPDATE imagen SET espacio = %s, etiquetas = %s WHERE huella = %s",
                         (f["espacio"], json.dumps(f["etiquetas"]), f["url"]))


@app.post("/api/migrar-fotos", dependencies=[Depends(autorizado)])
def migrar_fotos() -> dict:
    """Paso único: lleva las fotos y etiquetas del esquema por URL (foto, etiqueta) al de huellas
    (imagen, foto_url, etiqueta_img), descargando cada foto una vez para calcular su huella y
    guardarla en el NAS. Idempotente; borra las tablas viejas solo si todo cuadra."""
    with db.connect() as conn:
        if not conn.execute("SELECT to_regclass('foto') AS t").fetchone()["t"]:
            return {"estado": "ya migrado"}
        por_inmueble: dict = {}
        for r in conn.execute("SELECT url, inmueble FROM foto WHERE url NOT IN (SELECT url FROM foto_url)"):
            por_inmueble.setdefault(r["inmueble"], []).append(r["url"])
        sobrantes, fallidas = [], 0
        for inmueble, urls in por_inmueble.items():
            r = externos.vision("/analizar", {"urls": urls}) or {"fotos": []}
            fallidas += len(urls) - len(r["fotos"])
            for f in r["fotos"]:
                _, sobra = registrar_foto(conn, f, inmueble)
                if sobra:
                    sobrantes.append(sobra)
        if sobrantes:
            externos.vision("/borrar", {"archivos": sobrantes})
        # Etiquetas: de la URL a su foto real. Si dos copias tenían etiquetas distintas gana la más reciente.
        movidas = conn.execute("""
            INSERT INTO etiqueta_img (huella, cabeza, clase, creada)
            SELECT DISTINCT ON (fu.huella, e.cabeza) fu.huella, e.cabeza, e.clase, e.creada
            FROM etiqueta e JOIN foto_url fu ON fu.url = e.url
            ORDER BY fu.huella, e.cabeza, e.creada DESC
            ON CONFLICT (huella, cabeza) DO NOTHING""").rowcount
        resumen = conn.execute("""SELECT (SELECT count(*) FROM foto) AS urls_antes, (SELECT count(*) FROM foto_url) AS urls,
                                         (SELECT count(*) FROM imagen) AS fotos_reales,
                                         (SELECT count(*) FROM etiqueta) AS etiquetas_antes,
                                         (SELECT count(*) FROM etiqueta_img) AS etiquetas_ahora,
                                         (SELECT count(*) FROM etiqueta e LEFT JOIN foto_url fu ON fu.url = e.url
                                            LEFT JOIN etiqueta_img ei ON ei.huella = fu.huella AND ei.cabeza = e.cabeza
                                          WHERE ei.huella IS NULL) AS etiquetas_perdidas""").fetchone()
        completo = resumen["etiquetas_perdidas"] == 0 and fallidas == 0
        if completo:
            conn.execute("DROP TABLE etiqueta; DROP TABLE foto;")
        reclasificar_imagenes(conn)
    return {**resumen, "fallidas": fallidas, "etiquetas_movidas": movidas, "tablas_viejas_borradas": completo}


@app.get("/etiquetar")
def pagina_etiquetar() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "etiquetar.html")


# Historial para chrono ------------------------------------------------------------------------

@app.get("/api/historial.jsonl")
def historial() -> Response:
    """Eventos del mercado para chrono; `service` es el barrio catastral (ver mercado.historial)."""
    with db.connect() as conn:
        eventos = mercado.historial(conn)
    return Response("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in eventos), media_type="application/x-ndjson")


@app.get("/api/textos.jsonl")
def textos() -> Response:
    """Descripciones de los anuncios para `chrono similar` (ver mercado.textos)."""
    with db.connect() as conn:
        filas = mercado.textos(conn)
    return Response("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in filas), media_type="application/x-ndjson")


@app.post("/api/sospechas", dependencies=[Depends(autorizado)])
def sospechas(cuerpo: dict) -> dict:
    """Pares de anuncios con texto casi idéntico, hallados por chrono en el servidor."""
    nuevas = 0
    with db.connect() as conn:
        for s_ in cuerpo.get("pares", []):
            # Mismo texto, mismas habitaciones, área y precio en portales distintos: es el mismo
            # apartamento aunque las coordenadas difieran (cada portal geocodifica a su manera). El más
            # reciente pasa a duplicado del primero.
            dup = conn.execute("""
                UPDATE inmueble n SET duplicado_de = v.id
                FROM inmueble a, inmueble b, LATERAL (SELECT CASE WHEN a.visto_primero <= b.visto_primero THEN a ELSE b END AS v,
                                                             CASE WHEN a.visto_primero <= b.visto_primero THEN b ELSE a END AS nuevo) o,
                     LATERAL (SELECT (o.v).id) v(id)
                WHERE a.id = %(a)s AND b.id = %(b)s AND %(d)s <= 1 AND a.portal <> b.portal
                  AND a.habitaciones = b.habitaciones AND abs(a.area - b.area) <= 0.05 * greatest(a.area, b.area)
                  AND abs(a.total - b.total) <= 0.1 * greatest(a.total, b.total)
                  AND n.id = (o.nuevo).id AND n.duplicado_de IS NULL""",
                               {"a": s_["inmueble"], "b": s_["parecido"], "d": s_.get("distancia", 99)})
            if dup.rowcount:
                continue
            # Mismo apartamento en otro portal = esperado. Mismo anunciante y mismo apartamento (habitaciones,
            # área ±5 %, a menos de 150 m) = republicación. Mismo anunciante con otro apartamento = su
            # plantilla de texto, no se marca. Otro anunciante con el mismo texto = copia (típico de estafas).
            mismo_anunciante = f"{mercado.ANUNCIANTE.replace('i.', 'a.')} = {mercado.ANUNCIANTE.replace('i.', 'b.')}"
            # Republicar = el anterior ya no está (retirado o sin verse) cuando aparece el nuevo. Si los dos
            # siguen activos a la vez son unidades iguales del mismo edificio, no una republicación.
            mismo_apto = """a.habitaciones = b.habitaciones AND abs(a.area - b.area) <= 0.05 * greatest(a.area, b.area)
                            AND ST_DWithin(a.geom::geography, b.geom::geography, 150)
                            AND (b.retirado <= a.visto_primero OR b.visto_ultimo < a.visto_primero - interval '2 days'
                                 OR a.retirado <= b.visto_primero OR a.visto_ultimo < b.visto_primero - interval '2 days')"""
            r = conn.execute(f"""INSERT INTO sospecha (inmueble, parecido, distancia, motivo)
                                SELECT a.id, b.id, %(d)s,
                                       CASE WHEN {mismo_anunciante} THEN 'republicación del mismo apartamento'
                                            ELSE 'texto copiado de otro anunciante' END
                                FROM inmueble a, inmueble b
                                WHERE a.id = %(a)s AND b.id = %(b)s
                                  AND coalesce(a.duplicado_de, a.id) <> coalesce(b.duplicado_de, b.id)
                                  AND (NOT coalesce({mismo_anunciante}, false) OR coalesce({mismo_apto}, false))
                                ON CONFLICT DO NOTHING""",
                             {"a": s_["inmueble"], "b": s_["parecido"], "d": s_.get("distancia")})
            if r.rowcount:
                nuevas += 1
                pendientes.put(s_["inmueble"])
    return {"nuevas": nuevas}


@app.post("/api/metricas/registrar", dependencies=[Depends(autorizado)])
def registrar_metricas(cuerpo: dict | None = None) -> dict:
    with db.connect() as conn:
        return mercado.registrar_metricas(conn, (cuerpo or {}).get("externas"))


@app.post("/api/experimentos", dependencies=[Depends(autorizado)])
def experimento(cuerpo: dict) -> dict:
    """Marca cuándo se activa un cambio, para comparar sus métricas antes y después."""
    with db.connect() as conn:
        conn.execute("""INSERT INTO experimento (nombre, detalle) VALUES (%s, %s)
                        ON CONFLICT (nombre) DO UPDATE SET detalle = EXCLUDED.detalle""",
                     (cuerpo["nombre"], json.dumps(cuerpo.get("detalle"))))
    return {"ok": True}


@app.get("/api/metricas")
def metricas(dias: int = 30) -> dict:
    with db.connect() as conn:
        serie = conn.execute("""SELECT dia, nombre, clave, valor FROM metrica
                                WHERE dia >= current_date - %s ORDER BY dia, nombre, clave""", (dias,)).fetchall()
        experimentos = conn.execute("SELECT nombre, activado, detalle FROM experimento ORDER BY activado").fetchall()
        comparaciones = [c for c in (mercado.antes_y_despues(conn, x["nombre"], "demora_deteccion_min")
                                     for x in experimentos) if c]
    return {"serie": serie, "experimentos": experimentos, "comparaciones": comparaciones,
            "definiciones": {k: v[1] for k, v in mercado.METRICAS.items()}, "analisis": leer_analisis()}


def leer_analisis() -> dict | None:
    """Lo que calcula chrono en el servidor (servidor/analisis_chrono.py) y deja en el NAS."""
    p = Path(os.environ.get("ARRIENDO_CHRONO", "/chrono")) / "analisis.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


@app.get("/api/para-ti")
def para_ti() -> dict:
    with db.connect() as conn:
        datos = mercado.para_ti(conn, db.config())
        datos["personal"] = mercado.comparacion_personal(conn, comparar.privado(), comparar.referencia(conn))
        datos["por_pago"] = mercado.por_pago(conn, comparar.privado())
        datos["lista_corta"] = mercado.lista_corta(conn, comparar.privado())
        return datos


@app.get("/api/perfil")
def perfil() -> dict:
    """Lo que las páginas necesitan de tu configuración: destino y filtros de la búsqueda."""
    cfg = db.config()
    return {"destino": cfg["destino"], "busqueda": {k: cfg["busqueda"].get(k) for k in
            ("canon_total_max", "habitaciones_min", "banos_min", "tiempo_max_min")}}


@app.get("/api/recomendados")
def ver_recomendados(dias: int = 30) -> dict:
    with db.connect() as conn:
        datos = mercado.recomendados(conn, comparar.privado(), dias)
        datos["actual"] = comparar.referencia(conn)
        r = rotacion.analizar(conn)
        for i in datos["lista"]:
            i["historial"] = rotacion.historial_de(r, i["id"])
        datos["rotacion"] = {"medido_desde": r["medido_desde"], "dias_medidos": r["dias_medidos"],
                             "ventana_dias": rotacion.VENTANA_DIAS,
                             "sectores": sorted((s for s in r["sectores"].values() if "estabilidad_pct" in s),
                                                key=lambda s: -s["estabilidad_pct"])}
        return datos


@app.get("/api/referencia")
def ver_referencia(forzar: bool = False) -> dict:
    with db.connect() as conn:
        return {"actual": comparar.referencia(conn, forzar)}


@app.get("/api/resumen")
def ver_resumen() -> dict:
    with db.connect() as conn:
        return {"texto": mercado.resumen(conn, leer_analisis())}


@app.post("/api/resumen/enviar", dependencies=[Depends(autorizado)])
def enviar_resumen() -> dict:
    with db.connect() as conn:
        texto = mercado.resumen(conn, leer_analisis())
    return {"enviado": externos.telegram(texto), "texto": texto}


@app.get("/mercado")
def pagina_mercado() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "mercado.html")


@app.get("/recomendados")
def pagina_recomendados() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "recomendados.html")


@app.get("/salud")
def salud() -> dict:
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"ok": True, "en_cola": pendientes.qsize()}


@app.get("/")
def portada() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")
