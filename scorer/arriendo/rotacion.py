"""Historial de paso: cuánto rota la gente en cada edificio y en cada barrio, para buscar un lugar
donde quedarse años y no uno donde todos se van al cumplir el contrato.

- Edificio: la dirección con placa completa (cl 80 10 43); si el portal no la da, el punto del mapa
  redondeado a ~15 m, salvo que ese punto lo compartan muchos anuncios (centro del barrio).
- Unidad: edificio + habitaciones + área redondeada a 5 m². Una unidad «vuelve» cuando se publica
  otra vez después de que el anuncio anterior se retiró: alguien se fue.
- Barrio (sector catastral): anuncios publicados en los últimos 90 días por km² al mes, con la fecha
  de publicación del portal (Fincaraíz y Ciencuadras la dan; Metrocuadrado no). Solo se compara entre
  barrios que el radar busca: uno sin anuncios fuera de la búsqueda no es un barrio estable.

Con pocos días medidos, los «vuelve» y los retirados aún son pocos; la publicación (que viene del
portal) ya sirve desde el primer día.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from .mercado import _direccion

PLACA = re.compile(r"\b(cl|cr|dg|tv|av)\s+(\d+)\s*([a-z]?)\s*(?:bis\s*)?([a-z]?)\s+(\d+)\s*([a-z]?)\s+(\d+)\b")
VENTANA_DIAS = 90


def clave_edificio(direccion: str | None, lat: float | None, lon: float | None, puntos_comunes: set) -> str | None:
    m = PLACA.search(_direccion(direccion))
    if m:
        via, n1, l1, l2, n2, l3, n3 = m.groups()
        return f"dir:{via} {n1}{l1}{l2} {n2}{l3} {n3}"
    if lat is None:
        return None
    punto = (round(lat / 0.00015), round(lon / 0.00015))
    return None if punto in puntos_comunes else f"geo:{punto[0]}:{punto[1]}"


def cargar(conn) -> list[dict]:
    return conn.execute("""
        SELECT i.id, i.portal, i.url, i.total, i.area, i.habitaciones, i.sector, i.publicado, i.visto_primero,
               i.visto_ultimo, i.retirado, ST_Y(i.geom) AS lat, ST_X(i.geom) AS lon,
               coalesce(i.crudo->>'address', '') AS direccion, coalesce(s.nombre, i.barrio_portal) AS barrio
        FROM inmueble i LEFT JOIN sector s ON s.codigo = i.sector
        WHERE i.duplicado_de IS NULL
          AND NOT EXISTS (SELECT 1 FROM unnest(i.descartes) d WHERE d LIKE 'no es vivienda%%')""").fetchall()


def _inicio(f: dict) -> datetime:
    """Desde cuándo está publicado: la fecha del portal si la hay, si no la primera vez que se vio."""
    p = f["publicado"]
    if p is not None and not isinstance(p, datetime):
        p = datetime(p.year, p.month, p.day, tzinfo=timezone.utc)
    return min(x for x in (p, f["visto_primero"]) if x is not None)


def _fin(f: dict) -> datetime:
    return f["retirado"] or f["visto_ultimo"]


def agrupar(filas: list[dict]) -> tuple[dict, dict]:
    """{clave de edificio: [anuncios]} y {clave de unidad: [anuncios]}."""
    conteo = defaultdict(set)
    for f in filas:
        if f["lat"] is not None:
            conteo[(round(f["lat"] / 0.00015), round(f["lon"] / 0.00015))].add(_direccion(f["direccion"]) or f["id"])
    comunes = {p for p, dirs in conteo.items() if len(dirs) > 4}
    edificios, unidades = defaultdict(list), defaultdict(list)
    for f in filas:
        f["edificio"] = clave_edificio(f["direccion"], f["lat"], f["lon"], comunes)
        if f["edificio"]:
            edificios[f["edificio"]].append(f)
            unidad = f"{f['edificio']}|{f['habitaciones']}|{round((f['area'] or 0) / 5) * 5}"
            f["unidad"] = unidad
            unidades[unidad].append(f)
    return edificios, unidades


def vueltas(anuncios: list[dict]) -> int:
    """Veces que la misma unidad se volvió a publicar después de retirarse el anuncio anterior."""
    orden = sorted(anuncios, key=_inicio)
    n, fin = 0, None
    for a in orden:
        if fin is not None and a["visto_primero"] > fin + timedelta(days=2):
            n += 1
        fin = max(fin, _fin(a)) if fin else _fin(a)
    return n


def resumen_edificio(anuncios: list[dict], unidades: dict) -> dict:
    ahora = datetime.now(timezone.utc)
    claves = {a.get("unidad") for a in anuncios}
    cerrados = [(a["retirado"] - _inicio(a)).days for a in anuncios if a["retirado"]]
    return {
        "anuncios": len(anuncios),
        "unidades": len(claves),
        "vueltas": sum(vueltas(unidades[u]) for u in claves if u),
        "retirados": len(cerrados),
        "dias_hasta_arrendar": round(median(cerrados)) if cerrados else None,
        "primero": min(_inicio(a) for a in anuncios).date().isoformat(),
        "otros": sorted(({"id": a["id"], "url": a["url"], "total": a["total"], "area": a["area"],
                          "habitaciones": a["habitaciones"], "desde": _inicio(a).date().isoformat(),
                          "retirado": a["retirado"].date().isoformat() if a["retirado"] else None,
                          "dias": ((a["retirado"] or ahora) - _inicio(a)).days} for a in anuncios),
                        key=lambda x: x["desde"], reverse=True),
    }


def por_sector(conn, filas: list[dict], unidades: dict) -> dict:
    """Por barrio catastral: publicaciones recientes por km² al mes, retiros, unidades que vuelven y días publicados."""
    ahora = datetime.now(timezone.utc)
    areas = {r["codigo"]: r["area_km2"] for r in conn.execute(
        "SELECT codigo, area_km2 FROM sector WHERE codigo = ANY(%s)", (list({f["sector"] for f in filas if f["sector"]}),)).fetchall()}
    grupos = defaultdict(list)
    for f in filas:
        if f["sector"]:
            grupos[f["sector"]].append(f)
    salida = {}
    for codigo, fs in grupos.items():
        km2 = max(areas.get(codigo) or 0, 0.05)
        con_fecha = [f for f in fs if f["publicado"] is not None]
        recientes = [f for f in con_fecha if _inicio(f) >= ahora - timedelta(days=VENTANA_DIAS)]
        cerrados = [(f["retirado"] - _inicio(f)).days for f in fs if f["retirado"]]
        activos = [(ahora - _inicio(f)).days for f in fs if not f["retirado"]]
        claves_u = {f.get("unidad") for f in fs if f.get("unidad")}
        edif = defaultdict(set)
        for f in fs:
            if f.get("edificio"):
                edif[f["edificio"]].add(f.get("unidad"))
        salida[codigo] = {
            "sector": codigo, "barrio": fs[0]["barrio"], "anuncios": len(fs), "con_fecha": len(con_fecha), "activos": len(activos),
            "retirados": len(cerrados),
            "publicados_mes_km2": round(len(recientes) / km2 / (VENTANA_DIAS / 30), 1),
            "vueltas": sum(vueltas(unidades[u]) for u in claves_u),
            "edificios_varias_unidades": sum(1 for us in edif.values() if len(us) >= 2),
            "dias_hasta_arrendar": round(median(cerrados)) if cerrados else None,
            "dias_publicado_activos": round(median(activos)) if activos else None,
        }
    # Percentil entre los barrios con datos suficientes (≥ 3 anuncios con fecha del portal): 100 = el que más rota.
    con_datos = sorted((s for s in salida.values() if s["con_fecha"] >= 3), key=lambda s: s["publicados_mes_km2"])
    for k, s in enumerate(con_datos):
        s["rotacion_pct"] = round(100 * k / max(len(con_datos) - 1, 1))
        s["rotacion"] = "alta" if s["rotacion_pct"] >= 67 else "media" if s["rotacion_pct"] >= 33 else "baja"
    return salida


def analizar(conn) -> dict:
    filas = cargar(conn)
    edificios, unidades = agrupar(filas)
    sectores = por_sector(conn, filas, unidades)
    desde = min((f["visto_primero"] for f in filas), default=None)
    por_id = {f["id"]: f for f in filas}
    return {"filas": por_id, "edificios": edificios, "unidades": unidades, "sectores": sectores,
            "medido_desde": desde.date().isoformat() if desde else None,
            "dias_medidos": (datetime.now(timezone.utc) - desde).days if desde else 0}


def historial_de(analisis: dict, id_: str) -> dict:
    """Lo que se sabe del edificio y del barrio de un anuncio."""
    f = analisis["filas"].get(id_)
    if not f:
        return {}
    ed = analisis["edificios"].get(f.get("edificio")) or [f]
    resumen = resumen_edificio(ed, analisis["unidades"])
    resumen["otros"] = [o for o in resumen["otros"] if o["id"] != id_]
    resumen["por_direccion"] = (f.get("edificio") or "").startswith("dir:")
    return {"edificio": resumen, "sector": analisis["sectores"].get(f["sector"]),
            "publicado_hace": (datetime.now(timezone.utc) - _inicio(f)).days, "fecha_del_portal": f["publicado"] is not None}
