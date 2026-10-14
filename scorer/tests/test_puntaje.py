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
