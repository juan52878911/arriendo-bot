"""Filtros duros y puntaje 0-100 de un inmueble, con el detalle de cada componente."""

import re
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


_RANGOS = [(r"menos de 1", 0.5), (r"1 a 8", 4.5), (r"9 a 15", 12), (r"16 a 30", 23), (r"mas de 30", 40),
           (r"entre 0 y 5", 2.5), (r"entre 5 y 10", 7.5), (r"entre 10 y 20", 15), (r"mas de 20", 30)]


def antiguedad_anios(i: dict) -> float | None:
    """Edad aproximada del edificio según lo que publica cada portal."""
    crudo = i.get("crudo") or {}
    if isinstance(crudo, str):
        return None
    if i.get("portal") == "ciencuadras":
        try:
            return float(crudo.get("antiquity"))
        except (TypeError, ValueError):
            return None
    texto = ""
    if i.get("portal") == "fincaraiz":
        texto = next((str(t.get("value")) for t in crudo.get("technicalSheet") or [] if t.get("field") == "constructionYear"), "")
    elif i.get("portal") == "metrocuadrado":
        texto = next((str(f) for f in crudo.get("featured") or [] if "tiempoConstruido" in str(f)), "")
    texto = sin_tildes(texto)
    return next((v for patron, v in _RANGOS if re.search(patron, texto)), None)


def evaluar(i: dict, contexto: dict, cfg: dict) -> dict:
    """`i` es la fila de inmueble; `contexto` trae lo que sale de las capas (sector, mediana...).

    Devuelve {"puntaje", "detalle", "descartes", "alertas"}. Un componente sin dato puntúa 0,5
    y lo dice en su nota: un dato que falta no debe hundir ni salvar a un inmueble.
    """
    b, par, pesos = cfg["busqueda"], cfg["parametros"], cfg["pesos"]
    destino = (cfg.get("destino") or {}).get("nombre", "el destino")
    texto = sin_tildes(" ".join([i.get("titulo") or "", i.get("descripcion") or "",
                                 " ".join(i.get("facilidades") or [])]))
    descartes, alertas, detalle = [], [], {}

    # Filtros duros ------------------------------------------------------------------------
    # Las búsquedas de "apartamento" de algunos portales (Ciencuadras) también devuelven oficinas y locales.
    # Manda el tipo que publica el portal o la URL; el título no, porque para Ciencuadras lo armamos nosotros.
    crudo = i.get("crudo") if isinstance(i.get("crudo"), dict) else {}
    tipo_portal = sin_tildes(str(crudo.get("realEstateType") or (crudo.get("mtipoinmueble") or {}).get("nombre")
                                 or (crudo.get("property_type") or {}).get("name") or ""))
    no_vivienda = r"\b(oficina|local|bodega|consultorio|lote|parqueadero)\b"
    if re.search(no_vivienda, tipo_portal) or re.search(no_vivienda, sin_tildes(i.get("url") or "")):
        descartes.append("no es vivienda (oficina, local o similar)")
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
        descartes.append(f"{i['tiempo_min']} min a {destino}")

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

    # Estabilidad: percentil de la UPZ entre todas las de Bogotá (100 = donde menos hogares llegan cada año).
    est = contexto.get("estabilidad") or {}
    if est.get("estable_pct") is not None:
        poner("estabilidad", est["estable_pct"] / 100,
              f"{est['llegaron_12m_pct']:.1f} % de los hogares llegó en el último año, {est['propietarios_pct']:.0f} % propietarios "
              f"(más estable que el {est['estable_pct']:.0f} % de Bogotá)")
    else:
        poner("estabilidad", 0.5, "sin datos de la Encuesta Multipropósito para el barrio")

    if i.get("tiempo_min") is not None:
        poner("tiempo", _lineal(i["tiempo_min"], b["tiempo_max_min"], par["tiempo_ideal_min"]),
              f"{i['tiempo_min']} min a {destino} ({i.get('tiempo_fuente')})")
    else:
        poner("tiempo", 0.5, f"tiempo a {destino} sin calcular")

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

    # Aire y ruido: PM2.5 medido (SDA), ruido nocturno (mapa de ruido) y cercanía a vías principales.
    partes, notas = [], []
    if contexto.get("pm25") is not None:
        partes.append(_lineal(contexto["pm25"], par["pm25_malo"], par["pm25_bueno"]))
        notas.append(f"PM2.5 {contexto['pm25']:.0f} µg/m³ ({contexto.get('pm25_ano')})")
    if contexto.get("ruido_noche_db") is not None:
        partes.append(_lineal(contexto["ruido_noche_db"], par["ruido_malo_db"], par["ruido_bueno_db"]))
        notas.append(f"ruido nocturno ~{contexto['ruido_noche_db']:.0f} dB")
    if contexto.get("via_m") is not None:
        via = contexto["via_m"]
        partes.append(_lineal(via, par["via_cerca_m"], par["via_lejos_m"]))
        notas.append(f"{via:.0f} m a {contexto.get('via_nombre') or 'una vía principal'}")
        if via < par["via_encima_m"]:
            alertas.append(f"Parece estar sobre {contexto.get('via_nombre') or 'una vía principal'} "
                           f"({via:.0f} m): revisa ruido y aire en la visita")
    poner("aire_ruido", sum(partes) / len(partes) if partes else 0.5, ", ".join(notas) or "sin datos de aire ni ruido")

    partes, notas = [], []
    if contexto.get("parque_m") is not None:
        partes.append(_lineal(contexto["parque_m"], par["parque_lejos_m"], par["parque_cerca_m"]))
        notas.append(f"{contexto['parque_m']:.0f} m a {contexto.get('parque_nombre')}")
    if contexto.get("mercados") is not None:
        partes.append(min(1.0, contexto["mercados"] / 2))
        notas.append(f"{contexto['mercados']} supermercados o plazas a {par['mercado_radio_m']} m")
    if contexto.get("ciclorruta_m") is not None and contexto["ciclorruta_m"] < 300:
        notas.append(f"ciclorruta a {contexto['ciclorruta_m']:.0f} m")
    poner("entorno", sum(partes) / len(partes) if partes else 0.5, ", ".join(notas) or "sin datos del entorno")

    if contexto.get("siniestros_150m") and contexto["siniestros_150m"] >= par["siniestros_alerta"]:
        alertas.append(f"{contexto['siniestros_150m']} siniestros viales a menos de 150 m (2015-2021)")
    if contexto.get("suelo_m2") and detalle.get("precio_m2"):
        detalle["precio_m2"]["nota"] += f"; suelo de la manzana ${contexto['suelo_m2']:,.0f}/m² (Catastro)".replace(",", ".")

    # Servicios estables: lo que protege de cortes de agua, luz e internet, y un edificio no tan viejo.
    respaldo = _palabras(texto, cfg["palabras"].get("servicios", []))
    edad = antiguedad_anios(i)
    partes = [min(1.0, len(respaldo) / 2)]
    notas = [", ".join(respaldo) if respaldo else "el anuncio no menciona planta, tanque ni fibra"]
    if edad is not None:
        partes.append(_lineal(edad, 40, 10))
        notas.append(f"edificio de ~{edad:.0f} años")
    poner("servicios", sum(partes) / len(partes), "; ".join(notas))
    tranquilidad = _palabras(texto, cfg["palabras"].get("tranquilidad", []))
    if tranquilidad and "aire_ruido" in detalle:
        detalle["aire_ruido"]["valor"] = round(min(1.0, detalle["aire_ruido"]["valor"] + 0.15), 2)
        detalle["aire_ruido"]["nota"] += f"; {', '.join(tranquilidad)}"

    f = contexto.get("fotos")
    if f:
        partes, notas = [], []
        if f["horno"] is not None:
            cocina = [x for x in (f["horno"], f["gas"], f["extractor"]) if x is not None]
            partes.append(sum(cocina) / len(cocina))
            notas.append(f"cocina: horno {f['horno']:.0%}, gas {f['gas'] or 0:.0%}, extractor {f['extractor'] or 0:.0%}")
        else:
            partes.append(0.3)
            alertas.append("Las fotos no muestran la cocina: pide verla antes de ir")
        acabados = [x for x in (f["piso_bueno"], f["ducha_division"]) if x is not None]
        if acabados:
            partes.append(sum(acabados) / len(acabados))
            notas.append(f"piso porcelanato o laminado {f['piso_bueno'] or 0:.0%}, ducha con división {f['ducha_division'] or 0:.0%}")
        if f["luz"] is not None:
            partes.append(f["luz"])
            notas.append(f"luz {f['luz']:.0%}")
        valor = sum(partes) / len(partes)
        if (f["render"] or 0) > 0.7:
            valor *= 0.5
            alertas.append("Algunas fotos parecen renders, no fotos reales")
        poner("fotos", valor, f"{f['n']} fotos · " + "; ".join(notas))
    else:
        poner("fotos", 0.5, "fotos sin analizar")

    luz = _palabras(texto, cfg["palabras"]["luz"])
    if (i.get("piso") or 0) >= 4:
        luz.append(f"piso {i['piso']}")
    poner("luz", min(1.0, len(luz) / 2), ", ".join(luz) or "nada sobre luz o vista")

    for sp in contexto.get("sospechas") or []:
        if sp.get("motivo", "").startswith("republicación"):
            alertas.append(f"Es el mismo apartamento que {sp['parecido']}{' (ya retirado)' if sp['retirado'] else ''}: "
                           "se volvió a publicar, lleva más tiempo en el mercado de lo que parece")
        else:
            alertas.append(f"Su texto es idéntico al de otro anunciante ({sp['parecido']}): verifica que el "
                           "anunciante sea real antes de pagar nada")

    total_peso = sum(d["peso"] for d in detalle.values()) or 1
    puntaje = 100 * sum(d["valor"] * d["peso"] for d in detalle.values()) / total_peso
    return {"puntaje": round(puntaje, 1), "detalle": detalle, "descartes": descartes, "alertas": alertas}
