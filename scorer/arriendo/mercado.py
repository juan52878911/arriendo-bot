"""El mercado visto en el tiempo: lo que se le pasa a chrono, las métricas diarias y el resumen semanal.

- `historial()` y `textos()` son las dos fuentes JSONL que chrono indexa en el servidor
  (servidor/analisis_chrono.py): eventos del mercado por barrio y descripciones de los anuncios
  (chrono calcula su SimHash para encontrar textos casi idénticos).
- `registrar_metricas()` guarda cada día una foto de los números que dicen si un cambio mejoró algo.
- `resumen()` arma el aviso semanal.
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
