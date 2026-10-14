"""Servicios externos: tiempo en transporte público (Transitous) y avisos (Telegram)."""

import html
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("externos")

DESTINO = (4.5981, -74.0760)  # destino
BOGOTA = timezone(timedelta(hours=-5))
HTTP = httpx.Client(timeout=30, headers={"User-Agent": "arriendo-bot/0.1 (uso personal)"})


def proxima_salida() -> datetime:
    """Próximo día hábil a las 7:30 en Bogotá: la hora a la que se iría a la oficina."""
    dia = datetime.now(BOGOTA) + timedelta(days=1)
    while dia.weekday() >= 5:
        dia += timedelta(days=1)
    return dia.replace(hour=7, minute=30, second=0, microsecond=0)


def minutos_a_destino(lat: float, lon: float) -> int | None:
    """Duración del mejor itinerario en transporte público, o None si Transitous no responde."""
    try:
        r = HTTP.get("https://api.transitous.org/api/v1/plan", params={
            "fromPlace": f"{lat},{lon}", "toPlace": f"{DESTINO[0]},{DESTINO[1]}",
            "time": proxima_salida().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "numItineraries": 3})
        r.raise_for_status()
        duraciones = [it["duration"] for it in r.json().get("itineraries", [])]
        return round(min(duraciones) / 60) if duraciones else None
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("Transitous no respondió: %s", e)
        return None


def telegram(texto: str, foto: str | None = None) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.info("Telegram sin configurar; aviso solo en el log:\n%s", texto)
        return False
    base = f"https://api.telegram.org/bot{token}"
    try:
        if foto:
            r = HTTP.post(f"{base}/sendPhoto", data={"chat_id": chat, "photo": foto,
                                                     "caption": texto[:1024], "parse_mode": "HTML"})
            if r.is_success:
                return True
        r = HTTP.post(f"{base}/sendMessage", data={"chat_id": chat, "text": texto, "parse_mode": "HTML",
                                                   "disable_web_page_preview": "false"})
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.error("No se pudo avisar por Telegram: %s", e)
        return False


def e(texto) -> str:
    return html.escape(str(texto))
