"""El mercado visto en el tiempo: lo que se le pasa a chrono, las métricas diarias y el resumen semanal.

- `historial()` y `textos()` son las dos fuentes JSONL que chrono indexa en el servidor
  (servidor/analisis_chrono.py): eventos del mercado por barrio y descripciones de los anuncios
  (chrono calcula su SimHash para encontrar textos casi idénticos).
- `registrar_metricas()` guarda cada día una foto de los números que dicen si un cambio mejoró algo.
- `resumen()` arma el aviso semanal.
- `registrar_recomendados()` y `recomendados()`: lo que le gana a tu apartamento, día a día.
"""

import json
from datetime import date

# Inmobiliaria o anunciante según el portal; solo vive en el servidor, nunca en el repositorio.
ANUNCIANTE = """coalesce(i.crudo->'owner'->>'name', i.crudo->'data'->>'mnombrevisitor', i.crudo->>'userName')"""
# Hora exacta de publicación cuando el portal la da. Ciencuadras la marca como UTC pero es hora de Bogotá.
PUBLICADO = """CASE WHEN i.portal = 'ciencuadras' AND i.crudo->>'createdAt' IS NOT NULL
                    THEN ((i.crudo->>'createdAt')::timestamp AT TIME ZONE 'America/Bogota') END"""


def _pesos(n) -> str:
    return f"${n:,.0f}".replace(",", ".") if n is not None else "?"


def historial(conn) -> list[dict]:
    """Eventos: publicado (hora real) · nuevo (primera vez visto) · baja/sube · retirado (comprobado)."""
    filas = conn.execute(f"""
        WITH p AS (SELECT inmueble, visto, total, lag(total) OVER (PARTITION BY inmueble ORDER BY visto) AS antes
                   FROM precio),
        i AS (SELECT i.*, coalesce(s.nombre, i.barrio_portal, 'SIN ZONA') AS zona, {ANUNCIANTE} AS anunciante,
                     {PUBLICADO} AS publicado_exacto
              FROM inmueble i LEFT JOIN sector s ON s.codigo = i.sector)
        SELECT 'precio' AS fuente, p.inmueble, p.visto AS ts, p.total, p.antes, i.portal, i.area, i.habitaciones,
               i.zona, i.localidad, i.anunciante, i.publicado_exacto, i.visto_primero, i.retirado, i.duplicado_de
        FROM p JOIN i ON i.id = p.inmueble
        UNION ALL
        SELECT 'retiro', i.id, i.retirado, NULL, i.total, i.portal, i.area, i.habitaciones, i.zona, i.localidad,
               i.anunciante, i.publicado_exacto, i.visto_primero, i.retirado, i.duplicado_de
        FROM i WHERE i.retirado IS NOT NULL
        UNION ALL
        SELECT 'publicado', i.id, i.publicado_exacto, i.total, NULL, i.portal, i.area, i.habitaciones, i.zona,
               i.localidad, i.anunciante, i.publicado_exacto, i.visto_primero, i.retirado, i.duplicado_de
        FROM i WHERE i.publicado_exacto IS NOT NULL
        ORDER BY ts""").fetchall()
    arranque = conn.execute("SELECT min(visto_primero) + interval '1 hour' AS t FROM inmueble").fetchone()["t"]
    eventos = []
    for f in filas:
        extra = {}
        if f["fuente"] == "publicado":
            tipo, msg = "publicado", f"publicado {f['inmueble']} {_pesos(f['total'])}"
        elif f["fuente"] == "retiro":
            dias = (f["retirado"] - f["visto_primero"]).total_seconds() / 86400
            tipo, msg = "retirado", f"retirado {f['inmueble']} tras {dias:.1f} días (último {_pesos(f['antes'])})"
            extra["dias_en_mercado"] = round(dias, 2)
        elif f["antes"] is None:
            tipo = "nuevo"
            msg = f"nuevo {f['inmueble']} {_pesos(f['total'])} {f['habitaciones'] or '?'} hab {f['area'] or '?'} m²"
            # Demora de detección solo para lo publicado mientras el sistema ya vigilaba: lo anterior
            # al arranque no mide nada.
            if f["publicado_exacto"] and f["publicado_exacto"] >= arranque:
                extra["demora_min"] = round((f["visto_primero"] - f["publicado_exacto"]).total_seconds() / 60)
        else:
            tipo = "baja" if f["total"] < f["antes"] else "sube"
            msg = f"{tipo} {f['inmueble']} {_pesos(f['antes'])} → {_pesos(f['total'])}"
            extra["cambio_pct"] = round(100 * (f["total"] - f["antes"]) / f["antes"], 1)
        eventos.append({
            "id": f"{f['inmueble']}@{tipo}@{f['ts'].isoformat()}", "ts": f["ts"].isoformat(), "msg": msg,
            "level": tipo, "service": f["zona"], "localidad": f["localidad"], "portal": f["portal"],
            "inmueble": f["inmueble"], "total": f["total"] or f["antes"], "area": f["area"],
            "habitaciones": f["habitaciones"], "anunciante": f["anunciante"],
            "duplicado": f["duplicado_de"] is not None, **extra})
    return eventos


def textos(conn) -> list[dict]:
    """Una línea por anuncio con su descripción como mensaje: chrono le calcula el SimHash y
    `chrono similar` encuentra los casi idénticos (republicaciones, plantillas de estafa)."""
    filas = conn.execute(f"""
        SELECT i.id, i.visto_primero, i.portal, i.total, i.descripcion, {ANUNCIANTE} AS anunciante,
               coalesce(s.nombre, i.barrio_portal, 'SIN ZONA') AS zona
        FROM inmueble i LEFT JOIN sector s ON s.codigo = i.sector
        WHERE length(coalesce(i.descripcion, '')) >= 80
        ORDER BY i.visto_primero""").fetchall()
    return [{"id": f["id"], "ts": f["visto_primero"].isoformat(),
             "msg": " ".join(f["descripcion"].split())[:600], "level": "texto", "service": f["zona"],
             "portal": f["portal"], "total": f["total"], "anunciante": f["anunciante"]} for f in filas]


# Métricas diarias ---------------------------------------------------------------------------

METRICAS = {
    # nombre: (consulta que devuelve filas (clave, valor), qué mide)
    "nuevos_dia": ("""SELECT portal, count(*) FROM inmueble WHERE visto_primero >= now() - interval '1 day'
                      AND duplicado_de IS NULL GROUP BY portal""",
                   "anuncios únicos nuevos en las últimas 24 h, por portal (cobertura)"),
    "candidatos_dia": ("""SELECT '', count(*) FROM inmueble WHERE visto_primero >= now() - interval '1 day'
                          AND coalesce(cardinality(descartes), 0) = 0 AND duplicado_de IS NULL""",
                       "nuevos que cumplen los filtros duros"),
    "demora_deteccion_min": (f"""SELECT 'mediana', percentile_cont(0.5) WITHIN GROUP
                                   (ORDER BY extract(epoch FROM i.visto_primero - ({PUBLICADO})) / 60)
                                 FROM inmueble i WHERE i.portal = 'ciencuadras'
                                   AND ({PUBLICADO}) >= now() - interval '7 days'
                                   AND ({PUBLICADO}) >= (SELECT min(visto_primero) FROM inmueble) + interval '1 hour'""",
                             "minutos entre que Ciencuadras publica y el sistema lo ve (solo publicados tras el arranque)"),
    "dias_en_mercado": ("""SELECT 'mediana', percentile_cont(0.5) WITHIN GROUP
                             (ORDER BY extract(epoch FROM retirado - visto_primero) / 86400)
                           FROM inmueble WHERE retirado >= now() - interval '30 days'""",
                        "días entre que se ve un anuncio y se comprueba retirado (últimos 30 días)"),
    "bajadas_dia": ("""SELECT '', count(*) FROM (SELECT inmueble, total, lag(total) OVER w AS antes, visto FROM precio
                       WINDOW w AS (PARTITION BY inmueble ORDER BY visto)) p
                       WHERE antes IS NOT NULL AND total < antes AND visto >= now() - interval '1 day'""",
                    "bajadas de precio en 24 h"),
    "duplicados_pct": ("""SELECT '', 100.0 * count(duplicado_de) / greatest(count(*), 1) FROM inmueble""",
                       "porcentaje de anuncios que son el mismo apartamento en otro portal"),
    "fotos_duplicadas_pct": ("""SELECT '', 100.0 * (1 - count(DISTINCT huella)::float / greatest(count(*), 1)) FROM foto_url""",
                             "porcentaje de URLs de fotos que son una foto ya guardada"),
    "sospechas": ("""SELECT '', count(*) FROM sospecha WHERE creada >= now() - interval '1 day'""",
                  "textos casi idénticos a otro anuncio detectados por chrono en 24 h"),
}


def registrar_metricas(conn, externas: dict | None = None) -> dict:
    """Guarda las métricas de hoy (y las que mide chrono fuera del scorer, p. ej. errores en logs)."""
    hoy, guardadas = date.today(), {}
    filas = [(n, str(k), v) for n, (sql, _) in METRICAS.items()
             for k, v in ((r["k"], r["v"]) for r in conn.execute(f"SELECT * FROM ({sql}) AS q(k, v)").fetchall())]
    filas += [(n, str(k), v) for n, valores in (externas or {}).items() for k, v in valores.items()]
    for nombre, clave, valor in filas:
        if valor is None:
            continue
        conn.execute("""INSERT INTO metrica (dia, nombre, clave, valor) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (dia, nombre, clave) DO UPDATE SET valor = EXCLUDED.valor""",
                     (hoy, nombre, clave, float(valor)))
        guardadas.setdefault(nombre, {})[clave] = round(float(valor), 2)
    guardadas["recomendados"] = registrar_recomendados(conn)
    return guardadas


def antes_y_despues(conn, experimento: str, nombre: str, clave: str = "mediana") -> dict | None:
    """Promedio de una métrica antes y después de activar un experimento."""
    e = conn.execute("SELECT activado FROM experimento WHERE nombre = %s", (experimento,)).fetchone()
    if not e:
        return None
    r = conn.execute("""SELECT avg(valor) FILTER (WHERE dia < %(d)s::date) AS antes, count(*) FILTER (WHERE dia < %(d)s::date) AS n_antes,
                               avg(valor) FILTER (WHERE dia >= %(d)s::date) AS despues, count(*) FILTER (WHERE dia >= %(d)s::date) AS n_despues
                        FROM metrica WHERE nombre = %(n)s AND clave = %(c)s""",
                     {"d": e["activado"], "n": nombre, "c": clave}).fetchone()
    return {"experimento": experimento, "metrica": nombre, "activado": e["activado"].isoformat(), **r}


def resumen(conn, analisis: dict | None) -> str:
    """Texto del aviso semanal (HTML de Telegram)."""
    from .externos import e
    cfg_zonas = conn.execute("""SELECT count(*) FILTER (WHERE visto_primero >= now() - interval '7 days') AS nuevos,
                                       count(*) FILTER (WHERE visto_primero >= now() - interval '7 days'
                                                        AND coalesce(cardinality(descartes), 0) = 0) AS candidatos,
                                       count(*) FILTER (WHERE retirado >= now() - interval '7 days') AS retirados
                                FROM inmueble WHERE duplicado_de IS NULL""").fetchone()
    bajadas = conn.execute("""SELECT count(*) AS n FROM (SELECT total, lag(total) OVER w AS antes, visto FROM precio
                              WINDOW w AS (PARTITION BY inmueble ORDER BY visto)) p
                              WHERE antes IS NOT NULL AND total < antes AND visto >= now() - interval '7 days'""").fetchone()["n"]
    rapidos = conn.execute("""SELECT coalesce(s.nombre, i.barrio_portal) AS zona,
                                     percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM retirado - visto_primero) / 86400) AS dias,
                                     count(*) AS n
                              FROM inmueble i LEFT JOIN sector s ON s.codigo = i.sector
                              WHERE retirado >= now() - interval '30 days' GROUP BY 1 HAVING count(*) >= 3
                              ORDER BY 2 LIMIT 3""").fetchall()
    mejores = conn.execute("""SELECT round(puntaje) AS p, coalesce(s.nombre, i.barrio_portal) AS zona, total, url
                              FROM inmueble i LEFT JOIN sector s ON s.codigo = i.sector
                              WHERE coalesce(cardinality(descartes), 0) = 0 AND duplicado_de IS NULL AND retirado IS NULL
                                AND visto_primero >= now() - interval '7 days'
                              ORDER BY puntaje DESC NULLS LAST LIMIT 3""").fetchall()
    lineas = [f"📊 <b>Semana del radar</b>",
              f"{cfg_zonas['nuevos']} anuncios nuevos · {cfg_zonas['candidatos']} cumplen tus filtros · "
              f"{bajadas} bajadas de precio · {cfg_zonas['retirados']} retirados"]
    if rapidos:
        lineas.append("Se van más rápido: " + ", ".join(f"{e(r['zona'])} ({r['dias']:.0f} d)" for r in rapidos))
    pico = (analisis or {}).get("horas_pico")
    if pico:
        lineas.append(f"Horas de más publicaciones: {', '.join(f'{h}:00' for h in pico)}")
    errores = (analisis or {}).get("errores_logs_7d")
    if errores:
        lineas.append(f"Errores del sistema en 7 días: {errores}")
    if mejores:
        lineas.append("")
        lineas += [f'• <a href="{e(m["url"])}">{m["p"]:.0f}/100 · {e(m["zona"])} · {_pesos(m["total"])}</a>' for m in mejores]
    return "\n".join(lineas)


# Análisis para la búsqueda concreta ----------------------------------------------------------

def para_ti(conn, cfg: dict) -> dict:
    """Lo que el mercado ofrece para tus criterios (config.yaml): dónde, a qué precio, qué cambia si
    mueves el presupuesto o aceptas un baño, cuánta oferta nueva sale y con quién hablar."""
    b = cfg["busqueda"]
    tope, hab, banos = b["canon_total_max"], b["habitaciones_min"], b["banos_min"]
    excluidas = [str(x).upper() for x in b.get("localidades_excluidas", [])]
    base = """FROM inmueble i LEFT JOIN sector s ON s.codigo = i.sector
              WHERE i.duplicado_de IS NULL AND i.retirado IS NULL AND i.geom IS NOT NULL
                AND upper(unaccent(coalesce(i.localidad, ''))) <> ALL(%(excl)s)
                AND i.total BETWEEN 500000 AND 30000000
                AND lower(i.url) !~ '(oficina|local|bodega|consultorio)-en-'
                AND coalesce(i.crudo->>'realEstateType', 'Apartamento') !~* '(oficina|local|bodega|consultorio)'"""
    p = {"excl": excluidas or [""], "tope": tope, "hab": hab, "banos": banos}
    q = lambda sql: conn.execute(sql, p).fetchall()
    return {
        "criterios": {"tope": tope, "habitaciones": hab, "banos": banos, "tiempo_max": b["tiempo_max_min"]},
        "por_localidad": q(f"""SELECT coalesce(i.localidad, '?') AS localidad, count(*) AS n,
                                 percentile_cont(0.25) WITHIN GROUP (ORDER BY total)::int AS p25,
                                 percentile_cont(0.5) WITHIN GROUP (ORDER BY total)::int AS mediana,
                                 percentile_cont(0.5) WITHIN GROUP (ORDER BY area)::int AS m2,
                                 round(100.0 * count(*) FILTER (WHERE total <= %(tope)s) / count(*)) AS pct_en_presupuesto
                               {base} AND habitaciones >= %(hab)s AND banos >= %(banos)s
                               GROUP BY 1 HAVING count(*) >= 4 ORDER BY mediana"""),
        "sensibilidad": q(f"""SELECT t.tope, count(i.id) AS opciones
                              FROM (SELECT unnest(ARRAY[%(tope)s - 200000, %(tope)s, %(tope)s + 200000, %(tope)s + 400000,
                                                       %(tope)s + 600000, %(tope)s + 900000]) AS tope) t
                              LEFT JOIN LATERAL (SELECT i.id {base} AND habitaciones >= %(hab)s AND banos >= %(banos)s
                                                   AND total <= t.tope AND coalesce(tiempo_min, 0) <= {b['tiempo_max_min']}) i ON true
                              GROUP BY 1 ORDER BY 1"""),
        "con_un_bano": q(f"""SELECT count(*) AS n {base} AND habitaciones >= %(hab)s AND banos >= 1 AND total <= %(tope)s""")[0]["n"],
        "que_compra": q(f"""SELECT total <= %(tope)s AS dentro, count(*) AS n,
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY area)::int AS m2,
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(estrato, 0))::int AS estrato
                            {base} AND habitaciones >= %(hab)s GROUP BY 1 ORDER BY 1 DESC"""),
        "barrios": q(f"""SELECT coalesce(s.nombre, i.barrio_portal) AS barrio, i.localidad, count(*) AS n,
                           count(*) FILTER (WHERE total <= %(tope)s AND banos >= %(banos)s
                                              AND coalesce(cardinality(descartes), 0) = 0) AS en_presupuesto,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY total)::int AS mediana,
                           round(avg(puntaje)) AS puntaje, round(100 * (1 - max(s.seguridad_pct))) AS tranquilo
                         {base} AND habitaciones >= %(hab)s GROUP BY 1, 2 HAVING count(*) >= 3
                         ORDER BY en_presupuesto DESC, avg(puntaje) DESC LIMIT 15"""),
        "oferta_diaria": q("""SELECT visto_primero::date AS dia, count(*) AS n FROM inmueble
                              WHERE coalesce(cardinality(descartes), 0) = 0 AND duplicado_de IS NULL
                                AND visto_primero >= now() - interval '21 days' GROUP BY 1 ORDER BY 1"""),
        "anunciantes": q(f"""SELECT {ANUNCIANTE} AS anunciante, count(*) AS n
                             {base} AND habitaciones >= %(hab)s AND total <= %(tope)s GROUP BY 1 ORDER BY 2 DESC LIMIT 6"""),
        "bajadas": q("""SELECT count(*) AS n, round(avg(100.0 * (antes - total) / antes), 1) AS pct_promedio
                        FROM (SELECT total, lag(total) OVER (PARTITION BY inmueble ORDER BY visto) AS antes FROM precio) p
                        WHERE antes IS NOT NULL AND total < antes""")[0],
    }


def capacidad_compra(priv: dict) -> dict:
    """Préstamo y precio máximos con el salario y los supuestos de privado.yaml (cuota fija mensual)."""
    c, ing = priv["compra"], priv["ingresos"]
    r = (1 + c["tasa_efectiva_anual"] / 100) ** (1 / 12) - 1
    n = c["plazo_anios"] * 12
    factor = (1 - (1 + r) ** -n) / r
    cuota = ing["salario_mensual"] * c["pct_cuota_max_ingreso"] / 100
    prestamo = cuota * factor
    precio = prestamo / (1 - c["pct_cuota_inicial"] / 100)
    return {"cuota_max": round(cuota), "prestamo_max": round(prestamo), "precio_max": round(precio),
            "cuota_inicial": round(precio * c["pct_cuota_inicial"] / 100), "factor": factor, "supuestos": c}


def comparacion_personal(conn, priv: dict | None, ref: dict | None) -> dict | None:
    """Tu apartamento contra lo que hay; y si comprar es viable y en cuánto tiempo."""
    if not priv or not ref:
        return None
    vale = conn.execute("""SELECT round(puntaje) AS puntaje, coalesce(s.nombre, i.barrio_portal) AS barrio, i.total, i.area,
                                  i.url, i.comparacion
                           FROM inmueble i LEFT JOIN sector s ON s.codigo = i.sector
                           WHERE i.duplicado_de IS NULL AND i.retirado IS NULL AND i.comparacion IS NOT NULL
                             AND (i.comparacion->>'vale_la_pena')::boolean
                           ORDER BY i.puntaje DESC LIMIT 10""").fetchall()
    resumen = conn.execute("""SELECT count(*) FILTER (WHERE (comparacion->>'vale_la_pena')::boolean) AS valen,
                                     count(*) FILTER (WHERE coalesce(cardinality(descartes), 0) = 0) AS candidatos,
                                     count(*) AS comparados
                              FROM inmueble WHERE comparacion IS NOT NULL AND duplicado_de IS NULL AND retirado IS NULL""").fetchone()
    motivos = conn.execute("""SELECT comparacion->>'motivo' AS motivo, count(*) AS n FROM inmueble
                              WHERE comparacion IS NOT NULL AND duplicado_de IS NULL AND retirado IS NULL
                                AND coalesce(cardinality(descartes), 0) = 0
                              GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    cap = capacidad_compra(priv)
    area = priv["compra"]["area_objetivo_m2"]
    venta = conn.execute("""SELECT coalesce(localidad, '?') AS localidad, count(*) AS n,
                                   percentile_cont(0.5) WITHIN GROUP (ORDER BY precio / nullif(area, 0))::bigint AS m2,
                                   percentile_cont(0.25) WITHIN GROUP (ORDER BY precio / nullif(area, 0))::bigint AS m2_p25
                            FROM venta WHERE area > 30 AND precio BETWEEN 80000000 AND 3000000000
                              AND visto >= now() - interval '60 days'
                            GROUP BY 1 HAVING count(*) >= 3 ORDER BY 3""").fetchall()
    for v in venta:
        precio = v["m2"] * area
        precio_barato = v["m2_p25"] * area
        cuota_necesaria = precio * (1 - priv["compra"]["pct_cuota_inicial"] / 100) / cap["factor"]
        v.update(precio_objetivo=round(precio), precio_barato=round(precio_barato),
                 alcanza=precio_barato <= cap["precio_max"],
                 salario_necesario=round(cuota_necesaria / (priv["compra"]["pct_cuota_max_ingreso"] / 100)),
                 cuota_inicial=round(precio * priv["compra"]["pct_cuota_inicial"] / 100))
    return {"actual": ref, "resumen": resumen, "motivos": motivos, "vale_la_pena": vale,
            "compra": {"capacidad": {k: v for k, v in cap.items() if k != "factor"}, "area_objetivo": area, "por_localidad": venta}}


def por_pago(conn, priv: dict | None) -> dict | None:
    """Escenarios: para cada aporte posible del roomie y cada nivel de tu parte (cómodo, máximo, límite
    y el máximo futuro), cuántos apartamentos mejores que el tuyo caben y cuáles son los mejores."""
    if not priv:
        return None
    ing = priv["ingresos"]
    niveles = [("cómodo", ing["pago_comodo"]), ("máximo", ing["pago_max"]), ("límite", ing["pago_limite"])]
    if ing.get("futuro"):
        niveles.append((f"desde {ing['futuro']['desde']}", ing["futuro"]["pago_max"]))
    filas = conn.execute("""
        SELECT i.id, i.total, i.area, round(i.puntaje) AS puntaje, coalesce(s.nombre, i.barrio_portal) AS barrio, i.url
        FROM inmueble i LEFT JOIN sector s ON s.codigo = i.sector
        WHERE i.duplicado_de IS NULL AND i.retirado IS NULL AND (i.comparacion->>'mejor_que_actual')::boolean
        ORDER BY i.puntaje DESC""").fetchall()
    escenarios = []
    for aporte in sorted(set(ing.get("escenarios_aporte", []) + [ing.get("aporte_companero", 0)])):
        celdas = []
        for nombre, pago in niveles:
            tope = aporte + pago
            aqui = [f for f in filas if f["total"] <= tope]
            celdas.append({"nivel": nombre, "tu_parte": pago, "tope_total": tope, "opciones": len(aqui),
                           "mejores": [{"barrio": f["barrio"], "total": f["total"], "area": f["area"], "puntaje": f["puntaje"],
                                        "url": f["url"]} for f in aqui[:3]]})
        escenarios.append({"aporte": aporte, "actual": aporte == ing.get("aporte_companero"), "niveles": celdas})
    neto = ing.get("neto_mensual") or ing["salario_mensual"]
    pres = priv.get("presupuesto", {})
    return {"neto": neto, "niveles": [{"nivel": n, "pago": p, "pct_neto": round(100 * p / neto, 1)} for n, p in niveles],
            "escenarios": escenarios, "presupuesto": {k: sum(v.values()) for k, v in pres.items()},
            "nota": "Por encima del límite dejas de ahorrar o de pagar algo necesario. Si el roomie se va, el arriendo "
                    "completo queda a tu nombre: pídele un ingreso de al menos 2,5 veces su parte."}


def _direccion(texto: str | None) -> str:
    """'Calle 80 # 10 - 43 Apto 402' → 'cl 80 10 43 apto 402', para comparar direcciones escritas distinto."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\b(avenida calle|av calle|calle|cll|cl)\b\.?", " cl ", t)
    t = re.sub(r"\b(avenida carrera|av carrera|carrera|cra|kra|kr|cr|ak)\b\.?", " cr ", t)
    t = re.sub(r"\b(no|n°|nº)\b\.?|[#\-,.]", " ", t)
    t = re.sub(r"(\d)([a-z])\b", r"\1 \2", t)          # 80B → 80 b
    return " ".join(t.split())


def lista_corta(conn, priv: dict | None) -> list[dict]:
    """Las direcciones de tu lista corta: si el sistema las ve, a qué precio, desde cuándo y si cambiaron."""
    if not priv or not priv.get("lista_corta"):
        return []
    filas = conn.execute("""SELECT i.id, i.url, i.total, i.area, i.retirado, i.visto_primero, i.visto_ultimo, round(i.puntaje) AS puntaje,
                                   i.comparacion, i.descartes, coalesce(i.crudo->>'address', '') AS direccion,
                                   (SELECT count(*) FROM precio p WHERE p.inmueble = i.id) AS precios
                            FROM inmueble i WHERE coalesce(i.crudo->>'address', '') <> ''""").fetchall()
    for f in filas:
        f["norm"] = _direccion(f["direccion"])
    salida = []
    for entrada in priv["lista_corta"]:
        barrio, _, dir_ = entrada.partition(",")
        clave = _direccion(dir_ or barrio)
        hallados = [f for f in filas if clave and clave in f["norm"]]
        salida.append({"entrada": entrada, "encontrado": bool(hallados), "anuncios": [
            {k: f[k] for k in ("id", "url", "total", "area", "retirado", "visto_primero", "visto_ultimo", "puntaje", "precios")}
            | {"nivel": (f["comparacion"] or {}).get("nivel"), "mejor_que_actual": (f["comparacion"] or {}).get("mejor_que_actual"),
               "descartes": f["descartes"]}
            for f in hallados]})
    return salida


# Activos y sin duplicar que le ganan a tu apartamento (comparar.py). Los que se pasan de tu límite solo
# cuentan hasta el tope que alcanzarías más adelante (ingresos.futuro): Fincaraíz trae el mercado entero.
SUPERAN = """FROM inmueble i WHERE (i.comparacion->>'mejor_que_actual')::boolean
             AND i.retirado IS NULL AND i.duplicado_de IS NULL
             AND ((i.comparacion->>'vale_la_pena')::boolean OR i.total <= %(tope)s)"""


def tope_supera(priv: dict | None) -> int:
    """Total más alto que tiene sentido mostrar: tu máximo futuro (o tu límite) más lo que pone el roomie."""
    ing = (priv or {}).get("ingresos", {})
    tuyo = (ing.get("futuro") or {}).get("pago_max") or ing.get("pago_limite") or 0
    return tuyo + ing.get("aporte_companero", 0)


def registrar_recomendados(conn) -> dict:
    """La foto de hoy: se rehace, así lo que deja de ganarle a tu apartamento durante el día no queda
    contado. Lo que se retira queda marcado como «retirado» para saber que se fue."""
    from .comparar import privado
    hoy = date.today()
    conn.execute("""UPDATE recomendacion r SET tipo = 'retirado' FROM inmueble i
                    WHERE r.dia = %s AND i.id = r.inmueble AND i.retirado IS NOT NULL""", (hoy,))
    conn.execute("DELETE FROM recomendacion WHERE dia = %s AND tipo <> 'retirado'", (hoy,))
    conn.execute(f"""INSERT INTO recomendacion (dia, inmueble, tipo, puntaje, total, pago, nivel, gana_en)
                     SELECT %(hoy)s, i.id, CASE WHEN (i.comparacion->>'vale_la_pena')::boolean THEN 'recomendado' ELSE 'supera' END,
                            i.puntaje, i.total, (i.comparacion->>'pago_tuyo')::bigint, i.comparacion->>'nivel',
                            ARRAY(SELECT jsonb_array_elements_text(i.comparacion->'gana_en'))
                     {SUPERAN}
                     ON CONFLICT (dia, inmueble) DO NOTHING""", {"hoy": hoy, "tope": tope_supera(privado())})
    return {r["tipo"]: r["n"] for r in conn.execute(
        "SELECT tipo, count(*) AS n FROM recomendacion WHERE dia = %s GROUP BY tipo", (hoy,)).fetchall()}


def recomendados(conn, priv: dict | None, dias: int = 30) -> dict:
    """Lo que le gana a tu apartamento hoy, desde cuándo, y cómo ha cambiado la lista cada día."""
    lista = conn.execute(f"""
        SELECT i.id, i.portal, i.url, i.titulo, i.total, i.area, i.habitaciones, i.banos, i.estrato, i.parqueaderos,
               round(i.puntaje) AS puntaje, i.tiempo_min, i.barrio_portal, i.localidad, i.comparacion, i.alertas,
               (SELECT nombre FROM sector s WHERE s.codigo = i.sector) AS sector_nombre,
               i.imagenes[1] AS foto, ST_Y(i.geom) AS lat, ST_X(i.geom) AS lon,
               (SELECT min(dia) FROM recomendacion r WHERE r.inmueble = i.id) AS desde,
               (SELECT count(*) FROM recomendacion r WHERE r.inmueble = i.id) AS dias,
               (SELECT array_agg(total ORDER BY visto) FROM precio p WHERE p.inmueble = i.id) AS precios
        {SUPERAN}
        ORDER BY (i.comparacion->>'vale_la_pena')::boolean DESC, (i.comparacion->>'pago_tuyo')::bigint, i.puntaje DESC""",
        {"tope": tope_supera(priv)}).fetchall()
    serie = conn.execute("""SELECT dia, count(*) FILTER (WHERE tipo = 'recomendado') AS recomendados,
                                   count(*) FILTER (WHERE tipo = 'supera') AS superan
                            FROM recomendacion WHERE dia >= current_date - %s GROUP BY dia ORDER BY dia""", (dias,)).fetchall()
    # Cambios entre el último día registrado y el anterior: qué entró y qué salió (y por qué).
    ult = conn.execute("SELECT DISTINCT dia FROM recomendacion ORDER BY dia DESC LIMIT 2").fetchall()
    entraron, salieron = [], []
    if len(ult) == 2:
        hoy, ayer = ult[0]["dia"], ult[1]["dia"]
        entraron = [r["inmueble"] for r in conn.execute(
            """SELECT inmueble FROM recomendacion WHERE dia = %(h)s AND tipo <> 'retirado'
               EXCEPT SELECT inmueble FROM recomendacion WHERE dia = %(a)s""", {"h": hoy, "a": ayer}).fetchall()]
        salieron = conn.execute(
            """SELECT r.inmueble AS id, r.total, r.tipo, i.url, coalesce(s.nombre, i.barrio_portal) AS barrio,
                      CASE WHEN i.retirado IS NOT NULL THEN 'lo retiraron del portal'
                           WHEN i.duplicado_de IS NOT NULL THEN 'era un duplicado'
                           WHEN i.total > r.total THEN 'subió de precio'
                           ELSE coalesce(i.comparacion->>'motivo', 'ya no le gana a tu apartamento') END AS motivo
               FROM recomendacion r JOIN inmueble i ON i.id = r.inmueble LEFT JOIN sector s ON s.codigo = i.sector
               WHERE r.dia = %(a)s AND r.tipo <> 'retirado'
                 AND r.inmueble NOT IN (SELECT inmueble FROM recomendacion WHERE dia = %(h)s AND tipo <> 'retirado')""",
            {"h": hoy, "a": ayer}).fetchall()
    # Lo que estuvo recomendado esta semana y se comprobó retirado: se arrendó o lo bajaron.
    retirados = conn.execute("""
        SELECT DISTINCT ON (i.id) i.id, i.url, i.total, i.retirado, coalesce(s.nombre, i.barrio_portal) AS barrio,
               r.tipo, (SELECT min(dia) FROM recomendacion x WHERE x.inmueble = i.id) AS desde
        FROM recomendacion r JOIN inmueble i ON i.id = r.inmueble LEFT JOIN sector s ON s.codigo = i.sector
        WHERE r.dia >= current_date - 7 AND i.retirado >= now() - interval '7 days'
        ORDER BY i.id, r.dia DESC""").fetchall()
    return {"lista": lista, "serie": serie, "entraron": entraron, "salieron": salieron, "retirados": retirados,
            "dia": ult[0]["dia"] if ult else None, "tope_supera": tope_supera(priv),
            "futuro_desde": str(((priv or {}).get("ingresos", {}).get("futuro") or {}).get("desde") or "")}
