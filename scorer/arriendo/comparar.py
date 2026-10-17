"""Cada candidato contra donde vives hoy (config/privado.yaml): ¿le gana en lo que te importa, lo
puedes pagar y vale la pena ir a verlo?

La vivienda actual se mide con las mismas capas que los candidatos (seguridad del sector, PM2.5,
ruido nocturno, vías, tiempo al destino), usando el centro de su sector catastral: la dirección
exacta no sale del servidor.
"""

import json
import logging
from pathlib import Path

import yaml

from . import db, externos

log = logging.getLogger("comparar")


def privado() -> dict | None:
    p = db.CONFIG_DIR / "privado.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else None


def por_km2(incidentes: dict | None, area_km2: float | None) -> dict:
    """Riñas y hurtos del último año publicado, por km² del sector."""
    if not incidentes or not area_km2:
        return {}
    anio = max((k for v in incidentes.values() for k in v), default=None)
    km2 = max(area_km2, 0.05)
    return {f"{t}_km2": round(incidentes.get(t, {}).get(anio, 0) / km2) for t in ("rinas", "hurto")} | {"incidentes_anio": anio}


CAMPOS_EM = ("codigo", "nombre", "edicion", "hogares_muestra", "propietarios_pct", "arriendo_pct", "llegaron_12m_pct",
             "llegaron_12m_arriendo_pct", "permanencia_anios", "anios_propietarios", "arriendo_compraran_pct", "estable_pct")


def estabilidad_de(conn, sectores: list[str]) -> dict:
    """Encuesta Multipropósito de la UPZ (o grupo de UPZ) de cada sector catastral: {sector: {...}}."""
    if not sectores:
        return {}
    filas = conn.execute(f"""SELECT s.codigo AS sector, s.upz, {', '.join('e.' + c for c in CAMPOS_EM)}
                             FROM sector s JOIN estabilidad e ON s.upz = ANY(e.upz)
                             WHERE s.codigo = ANY(%s)""", (list(sectores),)).fetchall()
    return {f.pop("sector"): {"upz_grupo": f.pop("codigo"), **f} for f in filas}


def texto_estabilidad(e: dict) -> str:
    anios = f"~{e['permanencia_anios']:.0f} años de permanencia" if e.get("permanencia_anios") else "casi nadie llega"
    return (f"{e['llegaron_12m_pct']:.1f} % de los hogares llegó al barrio en el último año, {anios}, "
            f"{e.get('propietarios_pct') or 0:.0f} % propietarios")


def referencia(conn, forzar: bool = False) -> dict | None:
    """Los datos de la vivienda actual, calculados una vez y guardados en la tabla `referencia`."""
    priv = privado()
    if not priv:
        return None
    actual = priv["vivienda_actual"]
    fila = conn.execute("SELECT datos FROM referencia WHERE id = 'actual'").fetchone()
    if fila and not forzar and fila["datos"].get("sector") == actual["sector_catastral"] and "rinas_km2" in fila["datos"]:
        datos = fila["datos"]
        if not datos.get("estabilidad"):   # la encuesta se cargó después: se completa sin volver a medir todo
            datos["estabilidad"] = estabilidad_de(conn, [datos["sector"]]).get(datos["sector"])
            if datos["estabilidad"]:
                conn.execute("UPDATE referencia SET datos = %s WHERE id = 'actual'", (json.dumps(datos),))
        return datos
    s = conn.execute("""SELECT codigo, nombre, localidad, estrato_moda, seguridad_pct, periodo, incidentes, area_km2,
                               ST_Y(ST_PointOnSurface(geom)) AS lat, ST_X(ST_PointOnSurface(geom)) AS lon,
                               ST_PointOnSurface(geom) AS punto
                        FROM sector WHERE codigo = %s""", (actual["sector_catastral"],)).fetchone()
    if not s:
        log.warning("Sector %s de la vivienda actual no existe", actual["sector_catastral"])
        return None
    g = {"g": s["punto"]}
    aire = conn.execute("SELECT pm25 FROM aire WHERE ST_Contains(geom, %(g)s::geometry) LIMIT 1", g).fetchone()
    via = conn.execute("""SELECT ST_Distance(geom::geography, %(g)s::geometry::geography) AS m FROM via
                          ORDER BY geom <-> %(g)s::geometry LIMIT 1""", g).fetchone()
    datos = {
        "nombre": actual["nombre"], "sector": s["codigo"], "sector_nombre": s["nombre"], "localidad": s["localidad"],
        "estrato": s["estrato_moda"], "tranquilo_pct": round(100 * (1 - s["seguridad_pct"])),
        "pm25": aire["pm25"] if aire else None, "via_m": via["m"] if via else None,
        "ruido_noche_db": externos.ruido_noche_db(s["lat"], s["lon"]),
        "tiempo_min": externos.minutos_a_destino(s["lat"], s["lon"]),
        "area_m2": actual["area_m2"], "canon_total": actual["canon_total"], "parqueadero": actual.get("parqueadero", False),
        "servicios": "cortes frecuentes de agua, luz e internet",
        **por_km2(s["incidentes"], s["area_km2"]),
        "estabilidad": estabilidad_de(conn, [s["codigo"]]).get(s["codigo"]),
    }
    conn.execute("""INSERT INTO referencia (id, datos) VALUES ('actual', %s)
                    ON CONFLICT (id) DO UPDATE SET datos = EXCLUDED.datos, calculada = now()""", (json.dumps(datos),))
    return datos


def comparar(i: dict, ctx: dict, r: dict, ref: dict | None, priv: dict | None) -> dict | None:
    """Dimensión por dimensión: valor actual, valor del candidato y si mejora."""
    if not ref or not priv:
        return None
    v = priv.get("veredicto", {})
    dims = []

    def dim(nombre, actual, nuevo, mejora, texto):
        dims.append({"dimension": nombre, "actual": actual, "nuevo": nuevo, "mejora": mejora, "texto": texto})

    if ctx.get("seguridad_pct") is not None:
        nuevo = round(100 * (1 - ctx["seguridad_pct"]))
        dim("seguridad", ref["tranquilo_pct"], nuevo, nuevo >= ref["tranquilo_pct"] + v.get("mejora_seguridad_min", 20),
            f"más tranquilo que el {nuevo} % de los sectores (hoy {ref['tranquilo_pct']} %)")
    # Lo que más pesa en tu caso: peleas (riñas) y robos (hurtos), por km² del sector, contra el tuyo.
    if ctx.get("rinas_km2") is not None and ref.get("rinas_km2"):
        dim("peleas", ref["rinas_km2"], ctx["rinas_km2"], ctx["rinas_km2"] <= 0.6 * ref["rinas_km2"],
            f"{ctx['rinas_km2']} riñas/km² al año (hoy {ref['rinas_km2']})")
    if ctx.get("hurto_km2") is not None and ref.get("hurto_km2"):
        dim("robos", ref["hurto_km2"], ctx["hurto_km2"], ctx["hurto_km2"] <= ref["hurto_km2"],
            f"{ctx['hurto_km2']} hurtos/km² al año (hoy {ref['hurto_km2']})")
    if ctx.get("ruido_noche_db") is not None and ref.get("ruido_noche_db") is not None:
        dim("ruido", ref["ruido_noche_db"], ctx["ruido_noche_db"], ctx["ruido_noche_db"] <= ref["ruido_noche_db"] - 3,
            f"ruido nocturno ~{ctx['ruido_noche_db']:.0f} dB (hoy ~{ref['ruido_noche_db']:.0f} dB)")
    if ctx.get("pm25") is not None and ref.get("pm25") is not None:
        dim("aire", ref["pm25"], ctx["pm25"], ctx["pm25"] <= ref["pm25"] - 1,
            f"PM2.5 {ctx['pm25']:.0f} µg/m³ (hoy {ref['pm25']:.0f})")
    if i.get("tiempo_min") is not None and ref.get("tiempo_min") is not None:
        dim("tiempo", ref["tiempo_min"], i["tiempo_min"], i["tiempo_min"] < ref["tiempo_min"],
            f"{i['tiempo_min']} min al destino (hoy {ref['tiempo_min']})")
    if i.get("area"):
        dim("espacio", ref["area_m2"], i["area"], i["area"] >= ref["area_m2"],
            f"{i['area']:.0f} m² (hoy {ref['area_m2']})")
    # Si la gente se queda años en el barrio: hogares que llegaron en el último año (Encuesta Multipropósito).
    e_new, e_ref = ctx.get("estabilidad") or {}, ref.get("estabilidad") or {}
    if e_new.get("llegaron_12m_pct") is not None and e_ref.get("llegaron_12m_pct"):
        dim("estabilidad", e_ref["llegaron_12m_pct"], e_new["llegaron_12m_pct"],
            e_new["llegaron_12m_pct"] <= 0.8 * e_ref["llegaron_12m_pct"],
            f"{texto_estabilidad(e_new)} (hoy {texto_estabilidad(e_ref)})")
    serv = (r["detalle"].get("servicios") or {}).get("valor", 0)
    dim("servicios", "cortes frecuentes", round(serv, 2), serv >= 0.5,
        (r["detalle"].get("servicios") or {}).get("nota", ""))
    dim("parqueadero", ref["parqueadero"], (i.get("parqueaderos") or 0) > 0, (i.get("parqueaderos") or 0) > 0 and not ref["parqueadero"],
        "con parqueadero" if (i.get("parqueaderos") or 0) > 0 else "sin parqueadero")

    ing = priv.get("ingresos", {})
    pago = max(0, (i.get("total") or 0) - ing.get("aporte_companero", 0))
    base_pct = ing.get("neto_mensual") or ing.get("salario_mensual")
    pct = 100 * pago / base_pct if base_pct else None
    niveles = [("cómodo", ing.get("pago_comodo")), ("máximo", ing.get("pago_max")), ("límite", ing.get("pago_limite"))]
    nivel = next((n for n, tope in niveles if tope is not None and pago <= tope), "fuera de presupuesto")
    asequible = nivel != "fuera de presupuesto"
    ideal = nivel == "cómodo"
    gana = [d["dimension"] for d in dims if d["mejora"]]
    mejora = {d["dimension"]: d["mejora"] for d in dims}
    # Más seguro si el sector es claramente más tranquilo en conjunto, o si hay muchas menos peleas sin más robos.
    seguridad_ok = mejora.get("seguridad", False) or (mejora.get("peleas", False) and mejora.get("robos", False))
    espacio_ok = not i.get("area") or i["area"] >= ref["area_m2"] * v.get("area_min_pct", 80) / 100
    # «Mejor que tu casa» sin mirar el precio; luego, si además lo puedes pagar con tranquilidad.
    mejor = (seguridad_ok and espacio_ok and len(gana) >= v.get("dimensiones_min", 4)
             and not [d for d in r["descartes"] if not d.startswith("canon total")])
    vale = mejor and asequible and not r["descartes"]
    motivo = ("le gana en lo que te importa" if vale else
              ("menos peleas que hoy, pero más robos" if mejora.get("peleas") and not mejora.get("robos") else
               "no mejora lo suficiente la seguridad") if not seguridad_ok else
              "es mucho más pequeño que tu apartamento" if not espacio_ok else
              "se pasa de tu límite" if not asequible else
              "no cumple tus filtros" if r["descartes"] else
              f"solo mejora {len(gana)} de {len(dims)} cosas")
    return {"dimensiones": dims, "gana_en": gana, "total_dimensiones": len(dims),
            "pago_tuyo": pago, "pct_salario": round(pct, 1) if pct is not None else None,
            "pago_hoy": ref["canon_total"], "diferencia_mensual": pago - ref["canon_total"],
            "asequible": asequible, "ideal": ideal, "nivel": nivel, "mejor_que_actual": bool(mejor), "vale_la_pena": vale, "motivo": motivo}
