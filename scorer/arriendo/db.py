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


def config() -> dict:
    """Se relee en cada uso: cambiar config.yaml no exige reiniciar. Si existe privado.yaml, el tope del
    arriendo total sale de tu presupuesto: tu límite más lo que aporta el roomie."""
    cfg = yaml.safe_load((CONFIG_DIR / "config.yaml").read_text())
    priv = CONFIG_DIR / "privado.yaml"
    if priv.exists():
        ing = (yaml.safe_load(priv.read_text()) or {}).get("ingresos", {})
        if ing.get("pago_limite") is not None:
            cfg["busqueda"]["canon_total_max"] = ing["pago_limite"] + ing.get("aporte_companero", 0)
    return cfg


def fuentes() -> list[dict]:
    return yaml.safe_load((CONFIG_DIR / "fuentes.yaml").read_text())
