"""Descarga los datos abiertos de fuentes.yaml y los carga en PostGIS.

Uso:  python -m arriendo.ingesta            # solo las fuentes vencidas
      python -m arriendo.ingesta --forzar   # todas
"""

import collections
import io
import json
import logging
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone

import httpx

from . import db

log = logging.getLogger("ingesta")

HTTP = httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": "arriendo-bot/0.1 (uso personal)"})

# Tipos de incidente de la Secretaría de Seguridad: CM<TIPO><AA>CONT = conteo del año 20AA.
TIPOS = {"R": "rinas", "N": "narcoticos", "AOP": "orden_publico", "MM": "maltrato_mujer",
         "M": "maltrato", "D": "disparos", "PIA": "armas", "H": "hurto", "HC": "habitante_calle"}
GRAVES = ("hurto", "rinas", "disparos", "armas")
CONTEO = re.compile(r"^CM([A-Z]+?)(\d{2})CONT$")


def arcgis(url: str) -> list[dict]:
    """Todas las entidades de una capa ArcGIS REST, en GeoJSON y WGS84, paginando."""
    meta = HTTP.get(url, params={"f": "json"}).json()
    page = int(meta.get("maxRecordCount") or 1000)
    features, offset = [], 0
    while True:
        r = HTTP.get(f"{url}/query", params={
            "where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson",
            "resultOffset": offset, "resultRecordCount": page})
        r.raise_for_status()
        batch = r.json().get("features", [])
        features += batch
        if len(batch) < page:
            return features
        offset += page


def _geom(feature: dict) -> str | None:
    g = feature.get("geometry")
    return json.dumps(g) if g else None


def cargar_sector(fuente: dict, conn) -> int:
    n = 0
    for f in arcgis(fuente["url"]):
        p, geom = f["properties"], _geom(f)
        if not geom or not p.get("CMIUSCAT"):
            continue
        incidentes: dict = collections.defaultdict(dict)
        for campo, valor in p.items():
            m = CONTEO.match(campo)
            if m and m.group(1) in TIPOS and valor is not None:
                incidentes[TIPOS[m.group(1)]][f"20{m.group(2)}"] = valor
        conn.execute(
            """INSERT INTO sector (codigo, nombre, geom, area_km2, periodo, incidentes)
               VALUES (%(c)s, %(n)s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%(g)s), 4326)), NULL, %(p)s, %(i)s)
               ON CONFLICT (codigo) DO UPDATE SET nombre = EXCLUDED.nombre, geom = EXCLUDED.geom,
                 periodo = EXCLUDED.periodo, incidentes = EXCLUDED.incidentes""",
            {"c": p["CMIUSCAT"], "n": p["CMNOMSCAT"], "g": geom, "p": p.get("CMMES"),
             "i": json.dumps(incidentes)})
        n += 1
    # Densidad de incidentes graves del año más reciente y su percentil en la ciudad.
    conn.execute("UPDATE sector SET area_km2 = ST_Area(geom::geography) / 1e6")
    conn.execute(f"""
        WITH ultimo AS (
          SELECT max(k) AS anio FROM sector, jsonb_object_keys(incidentes->'hurto') k),
        d AS (
          SELECT codigo, ({' + '.join(f"coalesce((incidentes->'{t}'->>(SELECT anio FROM ultimo))::real, 0)" for t in GRAVES)})
                 / greatest(area_km2, 0.05) AS densidad
          FROM sector)
        UPDATE sector s SET densidad = d.densidad FROM d WHERE d.codigo = s.codigo""")
    conn.execute("""
        WITH r AS (SELECT codigo, percent_rank() OVER (ORDER BY densidad) AS pct FROM sector)
        UPDATE sector s SET seguridad_pct = r.pct FROM r WHERE r.codigo = s.codigo""")
    return n


def cargar_localidad(fuente: dict, conn) -> int:
    n = 0
    for f in arcgis(fuente["url"]):
        p, geom = f["properties"], _geom(f)
        if not geom:
            continue
        conn.execute(
            """INSERT INTO localidad (codigo, nombre, geom, hurto_personas_var, atributos)
               VALUES (%(c)s, %(n)s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%(g)s), 4326)), %(v)s, %(a)s)
               ON CONFLICT (codigo) DO UPDATE SET nombre = EXCLUDED.nombre, geom = EXCLUDED.geom,
                 hurto_personas_var = EXCLUDED.hurto_personas_var, atributos = EXCLUDED.atributos""",
            {"c": str(p["CMIULOCAL"]), "n": p["CMNOMLOCAL"], "g": geom, "v": p.get("CMHPVAR"),
             "a": json.dumps(p)})
        n += 1
    return n


def cargar_estratos(fuente: dict, conn) -> int:
    """Estrato por lote → distribución y estrato dominante de cada sector catastral.

    Los seis primeros dígitos del código de lote son el código del sector catastral.
    """
    r = HTTP.get(fuente["url"])
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    nombre = next(n for n in z.namelist() if n.lower().endswith(".csv"))
    por_sector: dict = collections.defaultdict(collections.Counter)
    lotes = 0
    with z.open(nombre) as f:
        texto = io.TextIOWrapper(f, encoding="utf-8-sig")
        next(texto)  # ESoCLote;ESoChip;ESoEstrato
        for linea in texto:
            partes = linea.strip().split(";")
            if len(partes) != 3 or not partes[2].isdigit():
                continue
            por_sector[partes[0][:6]][partes[2]] += 1
            lotes += 1
    for codigo, cuenta in por_sector.items():
        residencial = {e: c for e, c in cuenta.items() if e != "0"}
        moda = int(max(residencial, key=residencial.get)) if residencial else None
        conn.execute("UPDATE sector SET estratos = %s, estrato_moda = %s WHERE codigo = %s",
                     (json.dumps(dict(cuenta)), moda, codigo))
    return lotes


def cargar_estaciones(fuente: dict, conn) -> int:
    n = 0
    for f in arcgis(fuente["url"]):
        p, geom = f["properties"], _geom(f)
        if not geom:
            continue
        nombre = next((v for k, v in p.items() if "nombre" in k.lower() and isinstance(v, str)), None)
        ident = str(p.get("OBJECTID") or p.get("objectid") or n)
        conn.execute(
            """INSERT INTO estacion_tm (id, nombre, geom)
               VALUES (%s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
               ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre, geom = EXCLUDED.geom""",
            (ident, nombre, geom))
        n += 1
    return n


# Servidores públicos de Overpass; si uno está saturado se prueba el siguiente.
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
BBOX = "4.47,-74.23,4.84,-73.99"  # Bogotá urbana (sur, oeste, norte, este)


def overpass(consulta: str) -> list[dict]:
    ultimo = None
    for intento, servidor in enumerate(OVERPASS * 2):
        try:
            r = HTTP.post(servidor, data={"data": f"[out:json][timeout:180][bbox:{BBOX}];{consulta}"}, timeout=240)
            r.raise_for_status()
            return r.json()["elements"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            ultimo = e
            log.warning("Overpass %s falló (%s); pruebo otro", servidor, e)
            time.sleep(10 * (intento + 1))
    raise RuntimeError(f"Ningún servidor de Overpass respondió: {ultimo}")


def _linea(puntos: list[dict]) -> str:
    return json.dumps({"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in puntos]})


def cargar_osm(fuente: dict, conn) -> int:
    n = 0
    # Vías principales: lo que hace ruido y empeora el aire.
    for e in overpass('way[highway~"^(motorway|trunk|primary)$"];out geom;'):
        if len(e.get("geometry") or []) < 2:
            continue
        conn.execute("""INSERT INTO via (id, tipo, nombre, geom) VALUES (%s, %s, %s, ST_GeomFromGeoJSON(%s))
                        ON CONFLICT (id) DO UPDATE SET tipo = EXCLUDED.tipo, nombre = EXCLUDED.nombre, geom = EXCLUDED.geom""",
                     (f"w{e['id']}", e["tags"].get("highway"), e["tags"].get("name"), _linea(e["geometry"])))
        n += 1
    # Parques: vías cerradas y relaciones multipolígono, armadas en PostGIS con ST_BuildArea.
    for e in overpass('(way[leisure=park];relation[leisure=park];);out geom;'):
        if e["type"] == "way":
            lineas = [e.get("geometry") or []]
        else:
            lineas = [m.get("geometry") or [] for m in e.get("members", []) if m.get("role") in ("outer", "")]
        lineas = [l for l in lineas if len(l) >= 2]
        if not lineas:
            continue
        multi = json.dumps({"type": "MultiLineString",
                            "coordinates": [[[p["lon"], p["lat"]] for p in l] for l in lineas]})
        conn.execute("""
            WITH g AS (SELECT ST_Multi(ST_CollectionExtract(ST_BuildArea(ST_SetSRID(ST_GeomFromGeoJSON(%(m)s), 4326)), 3)) AS geom)
            INSERT INTO parque (id, nombre, area_m2, geom)
            SELECT %(id)s, %(nombre)s, ST_Area(geom::geography), geom FROM g WHERE NOT ST_IsEmpty(geom)
            ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre, area_m2 = EXCLUDED.area_m2, geom = EXCLUDED.geom""",
                     {"m": multi, "id": f"{e['type'][0]}{e['id']}", "nombre": e.get("tags", {}).get("name")})
        n += 1
    # Supermercados y plazas de mercado: para alguien que va a estudiar cocina.
    for e in overpass('(nwr[shop=supermarket];nwr[amenity=marketplace];);out center;'):
        lat, lon = (e["lat"], e["lon"]) if e["type"] == "node" else (e.get("center", {}).get("lat"), e.get("center", {}).get("lon"))
        if lat is None:
            continue
        tipo = "plaza_mercado" if e["tags"].get("amenity") == "marketplace" else "supermercado"
        conn.execute("""INSERT INTO poi (id, tipo, nombre, geom) VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                        ON CONFLICT (id) DO UPDATE SET tipo = EXCLUDED.tipo, nombre = EXCLUDED.nombre, geom = EXCLUDED.geom""",
                     (f"{e['type'][0]}{e['id']}", tipo, e["tags"].get("name"), lon, lat))
        n += 1
    # Ciclorrutas.
    for e in overpass('(way[highway=cycleway];way[cycleway~"^(track|lane)$"];);out geom;'):
        if len(e.get("geometry") or []) < 2:
            continue
        conn.execute("""INSERT INTO ciclorruta (id, geom) VALUES (%s, ST_GeomFromGeoJSON(%s))
                        ON CONFLICT (id) DO UPDATE SET geom = EXCLUDED.geom""", (f"w{e['id']}", _linea(e["geometry"])))
        n += 1
    return n


def cargar_aire(fuente: dict, conn) -> int:
    n = 0
    for f in arcgis(fuente["url"]):
        p, geom = f["properties"], _geom(f)
        if not geom or p.get("conc_pm25") is None:
            continue
        conn.execute("""INSERT INTO aire (id, pm25, ano, geom)
                        VALUES (%s, %s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))
                        ON CONFLICT (id) DO UPDATE SET pm25 = EXCLUDED.pm25, ano = EXCLUDED.ano, geom = EXCLUDED.geom""",
                     (str(p["id"]), p["conc_pm25"], p.get("ano"), geom))
        n += 1
    return n


def cargar_isocrona(fuente: dict, conn) -> int:
    """Zona desde la que se llega al destino en `minutos` o menos (llegada 8:15 de un día
    hábil): paradas alcanzables según Transitous + lo que se camina en el tiempo que sobra."""
    from .externos import DESTINO, proxima_salida
    minutos, metros_min, max_m = fuente.get("minutos", 45), 75, 800
    llegada = proxima_salida().replace(hour=8, minute=15)
    r = HTTP.get("https://api.transitous.org/api/v6/one-to-all", params={
        "one": f"{DESTINO[0]},{DESTINO[1]}", "time": llegada.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maxTravelTime": minutos, "maxPreTransitTime": 900, "arriveBy": "true",
        "transitModes": "TRANSIT", "preTransitModes": "WALK"})
    r.raise_for_status()
    puntos = [(e["place"]["lon"], e["place"]["lat"], min(max_m, (minutos - e["duration"]) * metros_min))
              for e in r.json().get("all", []) if e.get("duration", minutos) < minutos]
    puntos.append((DESTINO[1], DESTINO[0], min(max_m * 2, minutos * metros_min)))
    conn.execute("""
        WITH p AS (SELECT unnest(%s::float[]) AS lon, unnest(%s::float[]) AS lat, unnest(%s::float[]) AS r)
        INSERT INTO isocrona (id, minutos, geom)
        SELECT %s, %s, ST_Multi(ST_SimplifyPreserveTopology(ST_Union(
                 ST_Buffer(ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography, r)::geometry), 0.0002))
        FROM p WHERE r > 0
        ON CONFLICT (id) DO UPDATE SET geom = EXCLUDED.geom, minutos = EXCLUDED.minutos, calculada = now()""",
                 ([p[0] for p in puntos], [p[1] for p in puntos], [p[2] for p in puntos],
                  f"destino_{minutos}", minutos))
    return len(puntos)


LECTORES = {"seguridad_sector": cargar_sector, "delitos_localidad": cargar_localidad,
            "estratos": cargar_estratos, "estaciones_tm": cargar_estaciones, "osm": cargar_osm,
            "isocrona": cargar_isocrona, "aire_pm25": cargar_aire}


def run(forzar: bool = False) -> None:
    db.migrate()
    with db.connect() as conn:
        hechas = {r["id"]: r["actualizada"] for r in conn.execute("SELECT id, actualizada FROM fuente")}
        for fuente in db.fuentes():
            vence = hechas.get(fuente["id"])
            if not forzar and vence and datetime.now(timezone.utc) - vence < timedelta(days=fuente["frecuencia_dias"]):
                continue
            lector = LECTORES.get(fuente["id"])
            if lector is None:
                log.warning("Fuente %s sin lector en ingesta.py", fuente["id"])
                continue
            try:
                filas = lector(fuente, conn)
            except Exception:
                # Una fuente caída no tumba a las demás; se reintenta en la siguiente pasada.
                log.exception("Falló la fuente %s", fuente["id"])
                continue
            conn.execute("""INSERT INTO fuente (id, actualizada, filas) VALUES (%s, now(), %s)
                            ON CONFLICT (id) DO UPDATE SET actualizada = now(), filas = EXCLUDED.filas""",
                         (fuente["id"], filas))
            log.info("Fuente %s: %s filas", fuente["id"], filas)
        conn.execute("""UPDATE sector s SET localidad = l.nombre FROM localidad l
                        WHERE ST_Contains(l.geom, ST_PointOnSurface(s.geom))""")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run(forzar="--forzar" in sys.argv)
