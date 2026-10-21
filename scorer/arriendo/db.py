"""Conexión a PostGIS y utilidades de configuración."""

import os
from pathlib import Path

import psycopg
import yaml
from psycopg.rows import dict_row

CONFIG_DIR = Path(os.environ.get("ARRIENDO_CONFIG", "/config"))


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)


def migrate() -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text()
    with connect() as conn:
        conn.execute(schema)


def privado() -> dict:
    """config/privado.yaml: tus datos y criterios. No va al repositorio; si no existe, todo usa los valores
    generales de config.yaml."""
    p = CONFIG_DIR / "privado.yaml"
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def config() -> dict:
    """Se relee en cada uso: cambiar config.yaml o privado.yaml no exige reiniciar. Lo de privado.yaml
    manda: `destino` y `pesos` se reemplazan enteros (los pesos deben sumar 100), las demás secciones se
    completan clave a clave. El tope del arriendo total sale de tu presupuesto si lo hay: tu límite más
    lo que aporta quien comparte contigo."""
    cfg = yaml.safe_load((CONFIG_DIR / "config.yaml").read_text())
    priv = privado()
    for seccion in ("destino", "pesos"):
        if priv.get(seccion):
            cfg[seccion] = priv[seccion]
    for seccion in ("busqueda", "zonas_preferidas", "parametros", "avisos"):
        cfg.setdefault(seccion, {}).update(priv.get(seccion) or {})
    ing = priv.get("ingresos") or {}
    if ing.get("pago_limite") is not None:
        cfg["busqueda"]["canon_total_max"] = ing["pago_limite"] + ing.get("aporte_companero", 0)
    return cfg


def busquedas() -> list[dict]:
    """config/busquedas.json (privado): los barrios que se vigilan en cada portal. Lo usan Fredy
    (fredy/configurar.mjs) y la muestra de ventas."""
    p = CONFIG_DIR / "busquedas.json"
    import json
    return json.loads(p.read_text()).get("barrios", []) if p.exists() else []


def fuentes() -> list[dict]:
    return yaml.safe_load((CONFIG_DIR / "fuentes.yaml").read_text())
