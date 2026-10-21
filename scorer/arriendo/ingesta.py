"""Descarga los datos abiertos de fuentes.yaml y los carga en PostGIS.

Uso:  python -m arriendo.ingesta            # solo las fuentes vencidas
      python -m arriendo.ingesta --forzar   # todas
"""

import collections
import csv
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



def cargar_upz(fuente: dict, conn) -> int:
    """Polígonos de UPZ; Parque Entrenubes viene partido en dos localidades y se une en uno."""
    partes: dict = collections.defaultdict(list)
    for f in arcgis(fuente["url"]):
        p, geom = f["properties"], _geom(f)
        if geom and p.get("CODIGO_UPZ"):
            partes[str(p["CODIGO_UPZ"])].append((p["NOMBRE"], str(p.get("CODIGO_LOCALIDAD") or ""), geom))
    for codigo, ps in partes.items():
        conn.execute(
            """INSERT INTO upz (codigo, nombre, localidad, geom)
               SELECT %(c)s, %(n)s, %(l)s, ST_Multi(ST_Union(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(g), 4326))))
               FROM unnest(%(g)s::text[]) g
               ON CONFLICT (codigo) DO UPDATE SET nombre = EXCLUDED.nombre, localidad = EXCLUDED.localidad, geom = EXCLUDED.geom""",
            {"c": codigo, "n": ps[0][0].title(), "l": ps[0][1], "g": [g for _, _, g in ps]})
    return len(partes)


# Columnas de la Encuesta Multipropósito que hacen falta (diccionario EM 2021).
COLUMNAS_EM = ("MPIO", "COD_LOCALIDAD", "NOMBRE_LOCALIDAD", "COD_UPZ_GRUPO", "NOMBRE_UPZ_GRUPO", "FEX_C",
               "NPCEP6", "NPCEP16_1", "NPCEP16A", "NHCCP1", "NHCCP6", "NHCCP11", "NHCCP11A")


def _mediana_ponderada(pares: list[tuple[float, float]]) -> float | None:
    if not pares:
        return None
    pares = sorted(pares)
    mitad, acumulado = sum(w for _, w in pares) / 2, 0.0
    for valor, w in pares:
        acumulado += w
        if acumulado >= mitad:
            return valor
    return pares[-1][0]


def resumir_em(filas, anio: int) -> dict:
    """Personas de la encuesta → un resumen por UPZ o grupo de UPZ, con un hogar por jefe de hogar
    (NPCEP6 = 1) y pesos FEX_C. Solo Bogotá urbana (los rurales no tienen UPZ)."""
    z: dict = {}
    for f in filas:
        if f["MPIO"] != "11001" or f["NPCEP6"] != "1" or f["COD_UPZ_GRUPO"] in ("", "NA"):
            continue
        try:
            w = float(f["FEX_C"])
        except ValueError:
            continue
        d = z.setdefault(f["COD_UPZ_GRUPO"], {
            "nombre": f["NOMBRE_UPZ_GRUPO"].strip(), "localidad": f["NOMBRE_LOCALIDAD"].strip().title(),
            "n": 0, "w": 0.0, "propia": 0.0, "arriendo": 0.0, "resp": 0.0, "llego": 0.0,
            "resp_arr": 0.0, "llego_arr": 0.0, "compran": 0.0, "anios": []})
        tenencia = f["NHCCP1"]
        arrienda = tenencia == "3"
        d["n"] += 1
        d["w"] += w
        d["propia"] += w * (tenencia in ("1", "2"))
        d["arriendo"] += w * arrienda
        # Hace 12 meses: en este barrio (1) u otro barrio (2) del municipio, o en otro municipio/país (3, 4).
        if f["NPCEP16A"] in ("1", "2") or f["NPCEP16_1"] in ("3", "4"):
            llego = f["NPCEP16A"] == "2" or f["NPCEP16_1"] in ("3", "4")
            d["resp"] += w
            d["llego"] += w * llego
            if arrienda:
                d["resp_arr"] += w
                d["llego_arr"] += w * llego
        if arrienda and f["NHCCP11"] == "1" and f["NHCCP11A"] == "1":
            d["compran"] += w
        if tenencia in ("1", "2") and f["NHCCP6"].isdigit() and 1900 <= int(f["NHCCP6"]) <= anio:
            d["anios"].append((anio - int(f["NHCCP6"]), w))
    salida = {}
    for codigo, d in z.items():
        pct = lambda a, b: round(100 * a / b, 1) if b else None
        llegaron = pct(d["llego"], d["resp"])
        salida[codigo] = {
            "nombre": d["nombre"], "localidad": d["localidad"], "hogares_muestra": d["n"], "hogares": round(d["w"]),
            "propietarios_pct": pct(d["propia"], d["w"]), "arriendo_pct": pct(d["arriendo"], d["w"]),
            "llegaron_12m_pct": llegaron, "llegaron_12m_arriendo_pct": pct(d["llego_arr"], d["resp_arr"]),
            "permanencia_anios": round(100 / llegaron, 1) if llegaron else None,
            "anios_propietarios": _mediana_ponderada(d["anios"]),
            "arriendo_compraran_pct": pct(d["compran"], d["arriendo"]),
        }
    return salida


def cargar_estabilidad(fuente: dict, conn) -> int:
    """Lee los microdatos en streaming (no se guardan en disco) y deja un resumen por UPZ o grupo."""
    def filas():
        with HTTP.stream("GET", fuente["url"], timeout=httpx.Timeout(120, read=600)) as r:
            r.raise_for_status()
            lector = csv.reader(io.TextIOWrapper(_Flujo(r.iter_bytes()), encoding="latin-1", newline=""))
            cabecera = next(lector)
            ix = {c: cabecera.index(c) for c in COLUMNAS_EM}
            for fila in lector:
                if len(fila) == len(cabecera):
                    yield {c: fila[i] for c, i in ix.items()}
    resumen = resumir_em(filas(), fuente["anio"])
    if len(resumen) < 50:
        raise ValueError(f"La encuesta trae solo {len(resumen)} UPZ; no se reemplaza lo cargado")
    grupos = {k: [str(u) for u in v] for k, v in fuente.get("grupos", {}).items()}
    conn.execute("DELETE FROM estabilidad")
    for codigo, e in resumen.items():
        conn.execute(
            """INSERT INTO estabilidad (codigo, nombre, localidad, upz, edicion, hogares_muestra, hogares, propietarios_pct,
                 arriendo_pct, llegaron_12m_pct, llegaron_12m_arriendo_pct, permanencia_anios, anios_propietarios,
                 arriendo_compraran_pct)
               VALUES (%(codigo)s, %(nombre)s, %(localidad)s, %(upz)s, %(edicion)s, %(hogares_muestra)s, %(hogares)s,
                 %(propietarios_pct)s, %(arriendo_pct)s, %(llegaron_12m_pct)s, %(llegaron_12m_arriendo_pct)s,
                 %(permanencia_anios)s, %(anios_propietarios)s, %(arriendo_compraran_pct)s)""",
            e | {"codigo": codigo, "upz": grupos.get(codigo, [codigo]), "edicion": fuente["edicion"]})
    conn.execute("""WITH r AS (SELECT codigo, percent_rank() OVER (ORDER BY llegaron_12m_pct DESC) AS p
                               FROM estabilidad WHERE llegaron_12m_pct IS NOT NULL)
                    UPDATE estabilidad e SET estable_pct = round(100 * r.p) FROM r WHERE r.codigo = e.codigo""")
    return len(resumen)


class _Flujo(io.RawIOBase):
    """Bytes de una respuesta httpx como fichero, para leer el CSV sin bajarlo entero."""
    def __init__(self, trozos):
        self.trozos, self.resto = trozos, b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self.resto:
            try:
                self.resto = next(self.trozos)
            except StopIteration:
                return 0
        n = min(len(b), len(self.resto))
        b[:n], self.resto = self.resto[:n], self.resto[n:]
        return n


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
    """Zona desde la que se llega al destino en el tiempo máximo de tu búsqueda o menos (llegada 8:15
    de un día hábil): paradas alcanzables según Transitous + lo que se camina en el tiempo que sobra."""
    from .externos import destino, proxima_salida
    from .db import config
    d = destino()
    minutos, metros_min, max_m = config()["busqueda"].get("tiempo_max_min") or fuente.get("minutos", 45), 75, 800
    llegada = proxima_salida().replace(hour=8, minute=15)
    r = HTTP.get("https://api.transitous.org/api/v6/one-to-all", params={
        "one": f"{d['lat']},{d['lon']}", "time": llegada.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maxTravelTime": minutos, "maxPreTransitTime": 900, "arriveBy": "true",
        "transitModes": "TRANSIT", "preTransitModes": "WALK"})
    r.raise_for_status()
    puntos = [(e["place"]["lon"], e["place"]["lat"], min(max_m, (minutos - e["duration"]) * metros_min))
              for e in r.json().get("all", []) if e.get("duration", minutos) < minutos]
    puntos.append((d["lon"], d["lat"], min(max_m * 2, minutos * metros_min)))
    conn.execute("DELETE FROM isocrona")   # una sola zona: la del destino y el tiempo actuales
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


def cargar_venta(fuente: dict, conn) -> int:
    """Anuncios de venta de Fincaraíz (N+ habitaciones, una página por barrio vigilado en
    config/busquedas.json): referencia para comprar."""
    from .db import busquedas, config
    nav = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/131.0.0.0 Safari/537.36", "Accept-Language": "es-CO,es;q=0.9"}
    n = 0
    hab = config()["busqueda"].get("habitaciones_min") or 1
    for slug in [b["fincaraiz"] for b in busquedas() if b.get("fincaraiz")]:
        try:
            r = HTTP.get(f"https://www.fincaraiz.com.co/venta/apartamentos/{slug}/bogota/{hab}-o-mas-habitaciones?ordenListado=3",
                         headers=nav)
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            datos = json.loads(m.group(1))["props"]["pageProps"]["fetchResult"]["searchFast"]["data"] if m else []
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("Venta en %s falló: %s", slug, e)
            datos = []
        for d in datos:
            ficha = {t.get("field"): t.get("value") for t in d.get("technicalSheet") or []}
            num = lambda v: int(re.sub(r"[^0-9]", "", str(v))) if v and re.sub(r"[^0-9]", "", str(v)) else None
            precio = (d.get("price") or {}).get("amount")
            if not precio or not isinstance(d.get("latitude"), (int, float)):
                continue
            conn.execute("""INSERT INTO venta (id, barrio, precio, area, habitaciones, estrato, geom)
                            VALUES (%s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                            ON CONFLICT (id) DO UPDATE SET precio = EXCLUDED.precio, visto = now()""",
                         (f"fincaraiz:{d['id']}", slug, precio, d.get("m2apto") or d.get("m2Built") or d.get("m2"),
                          num(ficha.get("bedrooms")), num(ficha.get("stratum")), d["longitude"], d["latitude"]))
            n += 1
        time.sleep(3)
    conn.execute("""UPDATE venta v SET sector = s.codigo, localidad = s.localidad FROM sector s
                    WHERE v.sector IS NULL AND ST_Contains(s.geom, v.geom)""")
    return n


LECTORES = {"seguridad_sector": cargar_sector, "delitos_localidad": cargar_localidad,
            "estratos": cargar_estratos, "estaciones_tm": cargar_estaciones, "osm": cargar_osm,
            "isocrona": cargar_isocrona, "aire_pm25": cargar_aire,
            "venta_fincaraiz": cargar_venta, "upz": cargar_upz, "estabilidad_em": cargar_estabilidad}


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
        conn.execute("""UPDATE sector s SET upz = u.codigo FROM upz u
                        WHERE ST_Contains(u.geom, ST_PointOnSurface(s.geom)) AND s.upz IS DISTINCT FROM u.codigo""")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run(forzar="--forzar" in sys.argv)
