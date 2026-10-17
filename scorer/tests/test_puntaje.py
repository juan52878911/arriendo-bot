"""Pruebas sin base de datos: mapeo de Fincaraíz y reglas del puntaje.

    cd scorer && python -m pytest -q
"""

import json
from pathlib import Path

import yaml

from arriendo.app import desde_fincaraiz
from arriendo.puntaje import evaluar

AQUI = Path(__file__).parent
CFG = yaml.safe_load((AQUI.parent.parent / "config" / "config.yaml").read_text())
MUESTRA = json.loads((AQUI / "fincaraiz_muestra.json").read_text())


def base(**cambios) -> dict:
    i = {"titulo": "Apartamento", "descripcion": "", "facilidades": [], "total": 2_400_000, "area": 70,
         "habitaciones": 3, "banos": 2, "estrato": 3, "piso": 5, "parqueaderos": 0,
         "tiempo_min": None, "tiempo_fuente": None, "barrio_portal": "Alcázares"}
    i.update(cambios)
    return i


def test_mapea_la_ficha_tecnica():
    f = desde_fincaraiz(MUESTRA[0])
    assert f["id"] == f"fincaraiz:{MUESTRA[0]['id']}"
    assert f["url"].startswith("https://www.fincaraiz.com.co/")
    assert f["habitaciones"] == 3 and f["banos"] == 2 and f["estrato"] == 6
    assert f["lat"] and f["lon"] and f["total"] >= f["canon"]


def test_descarta_por_canon_habitaciones_y_localidad():
    r = evaluar(base(total=3_000_000, habitaciones=2), {"localidad": "LOS MÁRTIRES"}, CFG)
    assert len(r["descartes"]) == 3


def test_tiempo_estimado_no_descarta_pero_transitous_si():
    assert evaluar(base(tiempo_min=60, tiempo_fuente="estimado"), {}, CFG)["descartes"] == []
    assert evaluar(base(tiempo_min=60, tiempo_fuente="transitous"), {}, CFG)["descartes"]


def test_dato_faltante_puntua_neutro():
    r = evaluar(base(area=None), {}, CFG)
    assert r["detalle"]["area"]["valor"] == 0.5


def test_barrio_preferido_y_seguridad_suben_el_puntaje():
    malo = evaluar(base(barrio_portal="Kennedy"), {"seguridad_pct": 0.9}, CFG)["puntaje"]
    bueno = evaluar(base(), {"sector_nombre": "ALCAZARES", "seguridad_pct": 0.1}, CFG)["puntaje"]
    assert bueno > malo + 15


def test_precio_muy_bajo_alerta_estafa():
    r = evaluar(base(total=900_000, area=70), {"mediana_m2": 35_000, "mediana_n": 12}, CFG)
    assert any("estafa" in a for a in r["alertas"])


def test_variantes_de_una_palabra_cuentan_una_vez():
    r = evaluar(base(descripcion="cocina integral con gas natural y mesón"), {}, CFG)
    assert r["detalle"]["cocina"]["nota"] == "cocina integral, gas natural, meson"


def test_estudio_genera_alerta():
    r = evaluar(base(descripcion="3 alcobas, una es estudio"), {}, CFG)
    assert any("habitación" in a for a in r["alertas"])


def test_mapea_metrocuadrado():
    from arriendo.app import desde_metrocuadrado
    rec = json.loads((AQUI / "metrocuadrado_muestra.json").read_text())[0]
    f = desde_metrocuadrado(rec)
    assert f["id"] == f"metrocuadrado:{rec['midinmueble']}"
    assert f["total"] == rec["mvalorarriendo"] + int(rec["data"]["mvaloradministracion"])
    assert f["lat"] and f["habitaciones"] == 1 and f["estrato"] == 4
    assert "ascensor" in f["facilidades"] and "tipoEstufa Gas" in f["facilidades"]
    assert f["imagenes"][0].startswith("https://multimedia.metrocuadrado.com/")


def test_mapea_ciencuadras():
    from arriendo.app import desde_ciencuadras
    rec = json.loads((AQUI / "ciencuadras_muestra.json").read_text())[0]
    f = desde_ciencuadras(rec)
    assert f["id"] == f"ciencuadras:{rec['id']}"
    assert f["total"] == rec["rentPrice"] + rec["adminValue"]
    assert f["area"] == 45.0 and f["habitaciones"] == 2 and f["estrato"] == 3
    assert f["publicado"] == rec["createdAt"][:10] and f["lat"]


def test_avenida_cerca_alerta_y_baja_aire_ruido():
    r = evaluar(base(), {"via_m": 15, "via_nombre": "Avenida Carrera 30"}, CFG)
    assert r["detalle"]["aire_ruido"]["valor"] == 0
    assert any("Avenida Carrera 30" in a for a in r["alertas"])
    lejos = evaluar(base(), {"via_m": 400, "parque_m": 200, "parque_nombre": "Parque", "mercados": 3}, CFG)
    assert lejos["detalle"]["aire_ruido"]["valor"] == 1 and lejos["detalle"]["entorno"]["valor"] == 1


def test_pesos_suman_100():
    assert sum(CFG["pesos"].values()) == 100


def test_fotos_suben_o_bajan_y_avisan_renders():
    buenas = {"n": 8, "espacios": ["cocina"], "horno": 0.9, "gas": 0.9, "extractor": 0.8,
              "piso_bueno": 0.8, "ducha_division": 0.9, "luz": 0.8, "render": 0.1}
    r = evaluar(base(), {"fotos": buenas}, CFG)
    assert r["detalle"]["fotos"]["valor"] > 0.8
    renders = dict(buenas, render=0.9)
    r2 = evaluar(base(), {"fotos": renders}, CFG)
    assert r2["detalle"]["fotos"]["valor"] < 0.5 and any("render" in a for a in r2["alertas"])
    sin_cocina = dict(buenas, horno=None, gas=None, extractor=None)
    assert any("cocina" in a for a in evaluar(base(), {"fotos": sin_cocina}, CFG)["alertas"])


def test_correo_extrae_enlaces_de_anuncios_sin_seguir_terceros():
    from arriendo import correo
    texto = """<a href="https://www.fincaraiz.com.co/apartamento-en-arriendo-en-marly-bogota/190000001?utm=x">a</a>
    <a href="https://www.metrocuadrado.com/inmueble/arriendo-apartamento-bogota-marly-3-habitaciones/4186-M7100420">b</a>
    <a href="https://www.ciencuadras.com/inmueble/apartamento-en-arriendo-en-marly-bogota-3884375">c</a>
    <a href="https://www.facebook.com/fincaraiz">d</a>"""
    assert correo.enlaces_de_anuncios(texto, "fincaraiz") == {
        "https://www.fincaraiz.com.co/apartamento-en-arriendo-en-marly-bogota/190000001"}
    assert len(correo.enlaces_de_anuncios(texto, "metrocuadrado")) == 1
    assert len(correo.enlaces_de_anuncios(texto, "ciencuadras")) == 1


def test_sospecha_de_texto_copiado_alerta_y_republicacion_explica():
    copia = evaluar(base(), {"sospechas": [{"parecido": "x:1", "motivo": "texto copiado de otro anunciante", "retirado": False}]}, CFG)
    assert any("anunciante sea real" in a for a in copia["alertas"])
    repub = evaluar(base(), {"sospechas": [{"parecido": "x:2", "motivo": "republicación del mismo apartamento", "retirado": True}]}, CFG)
    assert any("se volvió a publicar" in a for a in repub["alertas"])


def test_descarta_oficinas_que_vienen_en_busquedas_de_apartamentos():
    r = evaluar(base(titulo="Oficina en arriendo en Ortezal", descripcion="",
                     url="https://www.ciencuadras.com/inmueble/oficina-en-arriendo-en-ortezal-bogota-1"), {}, CFG)
    assert any("no es vivienda" in d for d in r["descartes"])
    # Aunque el título diga apartamento (Ciencuadras), manda la URL o el tipo del portal.
    r2 = evaluar(base(titulo="Apartamento en arriendo en Alcázares", crudo={"realEstateType": "Consultorio"}), {}, CFG)
    assert any("no es vivienda" in d for d in r2["descartes"])
    assert not evaluar(base(titulo="Apartamento en arriendo en Ortezal"), {}, CFG)["descartes"]


REF = {"tranquilo_pct": 8, "ruido_noche_db": 67.5, "pm25": 15, "tiempo_min": 70, "area_m2": 60,
       "canon_total": 1000000, "parqueadero": False}
PRIV = {"ingresos": {"salario_mensual": 4000000, "aporte_companero": 800000, "pct_max_vivienda": 35},
        "veredicto": {"mejora_seguridad_min": 20, "area_min_pct": 80, "dimensiones_min": 4}}


def test_veredicto_vale_la_pena_cuando_gana_en_lo_importante():
    from arriendo.comparar import comparar
    i = base(total=2400000, area=72, tiempo_min=32, parqueaderos=1)
    r = evaluar(i, {}, CFG)
    c = comparar(i, {"seguridad_pct": 0.3, "ruido_noche_db": 57.5, "pm25": 11}, r, REF, PRIV)
    assert c["vale_la_pena"] and c["pago_tuyo"] == 1600000 and c["pct_salario"] == 32.0
    assert {"seguridad", "ruido", "aire", "tiempo", "espacio"} <= set(c["gana_en"])


def test_veredicto_no_si_no_mejora_seguridad_o_no_alcanza():
    from arriendo.comparar import comparar
    i = base(total=2400000, area=72, tiempo_min=32)
    r = evaluar(i, {}, CFG)
    inseguro = comparar(i, {"seguridad_pct": 0.85, "ruido_noche_db": 57.5, "pm25": 11}, r, REF, PRIV)
    assert not inseguro["vale_la_pena"] and "seguridad" in inseguro["motivo"]
    caro = comparar(base(total=2900000, area=72, tiempo_min=32), {"seguridad_pct": 0.3}, r, REF, PRIV)
    assert not caro["asequible"]


def test_mejor_que_actual_no_depende_del_precio():
    from arriendo.comparar import comparar
    priv = {"ingresos": {"salario_mensual": 4000000, "aporte_companero": 800000, "pago_ideal": 1000000, "pago_max": 1500000},
            "veredicto": {"mejora_seguridad_min": 20, "area_min_pct": 80, "dimensiones_min": 4}}
    i = base(total=2500000, area=72, tiempo_min=32)
    c = comparar(i, {"seguridad_pct": 0.3, "ruido_noche_db": 57.5, "pm25": 11}, evaluar(i, {}, CFG), REF, priv)
    assert c["mejor_que_actual"] and not c["asequible"] and not c["vale_la_pena"]   # paga 1,7 M > 1,5 M
