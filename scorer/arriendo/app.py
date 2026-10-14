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
from fastapi.responses import FileResponse

from . import db, externos, ingesta
from .externos import e
from .puntaje import evaluar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("scorer")

TOKEN = os.environ.get("ARRIENDO_TOKEN", "")
WEB_URL = os.environ.get("ARRIENDO_WEB_URL", "")
BASE_FINCARAIZ = "https://www.fincaraiz.com.co"
BOGOTA_BBOX = (-74.45, 4.45, -73.95, 4.85)

pendientes: "queue.Queue[str]" = queue.Queue()


@asynccontextmanager
async def ciclo(_: FastAPI):
    db.migrate()
    threading.Thread(target=trabajador, daemon=True).start()
    threading.Thread(target=ingesta_periodica, daemon=True).start()
    yield


app = FastAPI(title="arriendo-bot", lifespan=ciclo)


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
             descripcion = EXCLUDED.descripcion, canon = EXCLUDED.canon, total = EXCLUDED.total,
             administracion = EXCLUDED.administracion, area = EXCLUDED.area,
             habitaciones = EXCLUDED.habitaciones, banos = EXCLUDED.banos, estrato = EXCLUDED.estrato,
             piso = EXCLUDED.piso, parqueaderos = EXCLUDED.parqueaderos, facilidades = EXCLUDED.facilidades,
             imagenes = EXCLUDED.imagenes, geom = coalesce(EXCLUDED.geom, inmueble.geom),
             crudo = EXCLUDED.crudo, visto_ultimo = now()""", f)
    if f["total"] and (previo is None or previo["total"] != f["total"]):
        conn.execute("INSERT INTO precio (inmueble, total) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (f["id"], f["total"]))
        return True
    return False


@app.post("/raw", dependencies=[Depends(autorizado)])
def raw(cuerpo: dict) -> dict:
    if cuerpo.get("portal") != "fincaraiz":
        raise HTTPException(status_code=400, detail="portal no soportado")
    nuevos = 0
    with db.connect() as conn:
        for rec in cuerpo.get("records") or []:
            if rec.get("id") is None:
                continue
            f = desde_fincaraiz(rec)
            if guardar(conn, f):
                pendientes.put(f["id"])
                nuevos += 1
    return {"recibidos": len(cuerpo.get("records") or []), "a_puntuar": nuevos}


@app.post("/webhook", dependencies=[Depends(autorizado)])
def webhook(cuerpo: dict) -> dict:
    """Aviso HTTP de Fredy: solo se guarda el enlace a la ficha en Fredy."""
    with db.connect() as conn:
        for l in cuerpo.get("listings") or []:
            ident = str(l.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            if ident.isdigit() and l.get("fredyUrl"):
                conn.execute("UPDATE inmueble SET fredy_url = %s WHERE id = %s",
                             (l["fredyUrl"], f"fincaraiz:{ident}"))
    return {"ok": True}


# Puntaje -----------------------------------------------------------------------------------

def contexto(conn, i: dict, cfg: dict) -> dict:
    """Lo que dicen las capas del punto donde está el inmueble."""
    ctx: dict = {}
    if i["geom"] is None:
        return ctx
    s = conn.execute("""SELECT codigo, nombre, localidad, seguridad_pct, periodo, estrato_moda FROM sector
                        WHERE ST_Contains(geom, %s::geometry) LIMIT 1""", (i["geom"],)).fetchone()
    if s:
        ctx.update(sector=s["codigo"], sector_nombre=s["nombre"], localidad=s["localidad"],
                   seguridad_pct=s["seguridad_pct"], periodo=s["periodo"], estrato_moda=s["estrato_moda"])
    else:
        l = conn.execute("SELECT nombre FROM localidad WHERE ST_Contains(geom, %s::geometry) LIMIT 1",
                         (i["geom"],)).fetchone()
        ctx["localidad"] = l["nombre"] if l else None
    t = conn.execute("""SELECT nombre, ST_Distance(geom::geography, %(g)s::geometry::geography) AS m
                        FROM estacion_tm ORDER BY geom <-> %(g)s::geometry LIMIT 1""", {"g": i["geom"]}).fetchone()
    if t:
        ctx.update(tm_m=t["m"], tm_nombre=t["nombre"])
    minimo = cfg["parametros"]["muestra_min_precio"]
    for campo, valor in (("sector", ctx.get("sector")), ("localidad", ctx.get("localidad"))):
        if not valor:
            continue
        m = conn.execute(f"""SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY total / area) AS med, count(*) AS n
                             FROM inmueble WHERE {campo} = %s AND id <> %s AND area > 20 AND total > 0""",
                         (valor, i["id"])).fetchone()
        if m and m["n"] >= minimo:
            ctx.update(mediana_m2=m["med"], mediana_n=m["n"])
            break
    return ctx


def puntuar(id_: str) -> None:
    cfg = db.config()
    with db.connect() as conn:
        i = conn.execute("SELECT *, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM inmueble WHERE id = %s",
                         (id_,)).fetchone()
        if i is None:
            return
        ctx = contexto(conn, i, cfg)
        r = evaluar(i, ctx, cfg)
        # Transitous solo para los que sobreviven a los filtros baratos: una consulta por candidato.
        if not r["descartes"] and i["lat"] is not None and i["tiempo_min"] is None:
            minutos = externos.minutos_a_destino(i["lat"], i["lon"])
            if minutos is not None:
                i["tiempo_min"], i["tiempo_fuente"] = minutos, "transitous"
                r = evaluar(i, ctx, cfg)
            time.sleep(1)
        conn.execute("""UPDATE inmueble SET sector = %s, localidad = %s, tm_m = %s, tiempo_min = %s,
                          tiempo_fuente = %s, puntaje = %s, detalle = %s, descartes = %s, alertas = %s
                        WHERE id = %s""",
                     (ctx.get("sector"), ctx.get("localidad"), ctx.get("tm_m"), i["tiempo_min"],
                      i["tiempo_fuente"], r["puntaje"], json.dumps(r["detalle"], ensure_ascii=False),
                      r["descartes"], r["alertas"], id_))
        avisar(conn, i, ctx, r, cfg)


def avisar(conn, i: dict, ctx: dict, r: dict, cfg: dict) -> None:
    if r["descartes"] or r["puntaje"] < cfg["avisos"]["umbral"]:
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
        f"{i['tiempo_min']} min al destino" if i["tiempo_min"] else None] if x)
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


def ingesta_periodica() -> None:
    while True:
        try:
            ingesta.run()
        except Exception:
            log.exception("La ingesta falló; se reintenta en 6 h")
            time.sleep(6 * 3600)
            continue
        time.sleep(24 * 3600)


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
    filtro = "" if todos else "WHERE coalesce(cardinality(descartes), 0) = 0 AND puntaje IS NOT NULL"
    with db.connect() as conn:
        return conn.execute(f"""
            SELECT id, url, fredy_url, titulo, total, area, habitaciones, banos, estrato, puntaje, detalle,
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


@app.get("/api/fuentes")
def fuentes() -> list[dict]:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM fuente ORDER BY id").fetchall()


@app.get("/salud")
def salud() -> dict:
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"ok": True, "en_cola": pendientes.qsize()}


@app.get("/")
def portada() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")
