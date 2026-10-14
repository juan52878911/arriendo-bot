"""Filtros duros y puntaje 0-100 de un inmueble, con el detalle de cada componente."""

import unicodedata


def sin_tildes(texto: str | None) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower()


def _lineal(x: float, cero: float, uno: float) -> float:
    """0 en `cero`, 1 en `uno`, recortado a [0, 1]; sirve en ambos sentidos."""
    if uno == cero:
        return 1.0
    return max(0.0, min(1.0, (x - cero) / (uno - cero)))


def _palabras(texto: str, lista: list[str]) -> list[str]:
    """Las de `lista` que aparecen, sin repetir variantes: "mesón" y "meson" cuentan una vez, y
    "gas" no cuenta aparte si ya contó "gas natural"."""
    halladas = {sin_tildes(p) for p in lista if sin_tildes(p) in texto}
    return sorted(p for p in halladas if not any(p != q and p in q for q in halladas))


def evaluar(i: dict, contexto: dict, cfg: dict) -> dict:
    """`i` es la fila de inmueble; `contexto` trae lo que sale de las capas (sector, mediana...).

    Devuelve {"puntaje", "detalle", "descartes", "alertas"}. Un componente sin dato puntúa 0,5
    y lo dice en su nota: un dato que falta no debe hundir ni salvar a un inmueble.
    """
    b, par, pesos = cfg["busqueda"], cfg["parametros"], cfg["pesos"]
    texto = sin_tildes(" ".join([i.get("titulo") or "", i.get("descripcion") or "",
                                 " ".join(i.get("facilidades") or [])]))
    descartes, alertas, detalle = [], [], {}

    # Filtros duros ------------------------------------------------------------------------
    habitaciones = i.get("habitaciones")
    falsas = _palabras(texto, b.get("habitacion_falsa", []))
    if falsas and habitaciones:
        alertas.append(f"La descripción menciona {', '.join(falsas)}: puede que una habitación no lo sea")
    if i.get("total") and i["total"] > b["canon_total_max"]:
        descartes.append(f"canon total {i['total']:,} > {b['canon_total_max']:,}".replace(",", "."))
    if habitaciones is not None and habitaciones < b["habitaciones_min"]:
        descartes.append(f"{habitaciones} habitaciones")
    if i.get("banos") is not None and i["banos"] < b["banos_min"]:
        descartes.append(f"{i['banos']} baños")
    if contexto.get("localidad") and sin_tildes(contexto["localidad"]).upper() in {
            sin_tildes(l).upper() for l in b.get("localidades_excluidas", [])}:
        descartes.append(f"localidad excluida: {contexto['localidad']}")
    if i.get("tiempo_min") is not None and i.get("tiempo_fuente") == "transitous" \
            and i["tiempo_min"] > b["tiempo_max_min"]:
        descartes.append(f"{i['tiempo_min']} min al destino")

    # Componentes ---------------------------------------------------------------------------
    def poner(nombre: str, valor: float, nota: str) -> None:
        detalle[nombre] = {"valor": round(valor, 2), "peso": pesos.get(nombre, 0), "nota": nota}

    sector = sin_tildes(contexto.get("sector_nombre")).upper()
    barrio = sin_tildes(i.get("barrio_portal")).upper()
    zonas = [sin_tildes(z).upper() for z in cfg["zonas_preferidas"]["sectores"]]
    locs = [sin_tildes(z).upper() for z in cfg["zonas_preferidas"]["localidades"]]
    if any(sector.startswith(z) or barrio.startswith(z) for z in zonas):
        poner("zona", 1.0, f"barrio preferido ({contexto.get('sector_nombre') or i.get('barrio_portal')})")
    elif sin_tildes(contexto.get("localidad")).upper() in locs:
        poner("zona", 0.5, f"localidad preferida ({contexto.get('localidad')})")
    else:
        poner("zona", 0.0, contexto.get("sector_nombre") or i.get("barrio_portal") or "sin zona")

    mediana = contexto.get("mediana_m2")
    if i.get("total") and i.get("area") and mediana:
        m2 = i["total"] / i["area"]
        ratio = m2 / mediana
        poner("precio_m2", _lineal(ratio, 1.3, 0.7),
              f"${m2:,.0f}/m² frente a ${mediana:,.0f}/m² de la zona ({contexto['mediana_n']} inmuebles)".replace(",", "."))
        if ratio < par["estafa_ratio"]:
            alertas.append("Precio muy por debajo de la zona: posible estafa. No pagues nada antes de visitar.")
    else:
        poner("precio_m2", 0.5, "sin muestra suficiente en la zona todavía")

    pct = contexto.get("seguridad_pct")
    if pct is not None:
        poner("seguridad", 1 - pct,
              f"{contexto.get('sector_nombre')}: más tranquilo que el {100 - pct * 100:.0f} % de los sectores "
              f"({contexto.get('periodo')})")
    else:
        poner("seguridad", 0.5, "sin datos de seguridad del sector")

    if i.get("tiempo_min") is not None:
        poner("tiempo", _lineal(i["tiempo_min"], b["tiempo_max_min"], par["tiempo_ideal_min"]),
              f"{i['tiempo_min']} min al destino ({i.get('tiempo_fuente')})")
    else:
        poner("tiempo", 0.5, "tiempo al destino sin calcular")

    if i.get("area"):
        poner("area", _lineal(i["area"], par["area_min_m2"], par["area_buena_m2"]), f"{i['area']:.0f} m²")
    else:
        poner("area", 0.5, "área no publicada")

    estrato = i.get("estrato") or contexto.get("estrato_moda")
    if estrato:
        poner("estrato", par["estratos"].get(str(estrato), 0.5), f"estrato {estrato}")
    else:
        poner("estrato", 0.5, "estrato desconocido")

    cocina = _palabras(texto, cfg["palabras"]["cocina"])
    poner("cocina", min(1.0, len(cocina) / 3), ", ".join(cocina) or "el anuncio no describe la cocina")

    amen = _palabras(texto, cfg["palabras"]["amenidades"])
    if (i.get("parqueaderos") or 0) > 0:
        amen.append("parqueadero")
    poner("amenidades", min(1.0, len(amen) / 3), ", ".join(amen) or "sin amenidades mencionadas")

    if contexto.get("tm_m") is not None:
        poner("transporte", _lineal(contexto["tm_m"], par["tm_lejos_m"], par["tm_buena_m"]),
              f"{contexto['tm_m']:.0f} m a {contexto.get('tm_nombre') or 'una estación troncal'}")
    else:
        poner("transporte", 0.5, "sin datos de estaciones")

    luz = _palabras(texto, cfg["palabras"]["luz"])
    if (i.get("piso") or 0) >= 4:
        luz.append(f"piso {i['piso']}")
    poner("luz", min(1.0, len(luz) / 2), ", ".join(luz) or "nada sobre luz o vista")

    total_peso = sum(d["peso"] for d in detalle.values()) or 1
    puntaje = 100 * sum(d["valor"] * d["peso"] for d in detalle.values()) / total_peso
    return {"puntaje": round(puntaje, 1), "detalle": detalle, "descartes": descartes, "alertas": alertas}
