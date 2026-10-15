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
