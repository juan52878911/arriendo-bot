#!/usr/bin/env python3
"""arriendo-bot: chrono como cerebro histórico del servicio. Corre cada hora en el LXC (cron.hourly).

Cada hora:
  1. Baja de los contenedores los registros nuevos (logs.jsonl) y trae del scorer el historial del
     mercado (historial.jsonl) y las descripciones (textos.jsonl); `chrono sync` los indexa.
  2. Calcula con chrono las horas de más publicaciones y ajusta el intervalo de Fredy: más seguido en
     las horas pico, más espaciado el resto (experimento "horario_adaptativo").
  3. Busca con `chrono similar` textos casi idénticos entre anuncios nuevos y los manda al scorer.
  4. Deja todo en /data/chrono/analisis.json (lo lee el scorer para el mapa y el resumen).
Una vez al día (3 a. m.) registra las métricas del día; los lunes a las 7 a. m. manda el resumen.

Los resultados de las tareas programadas van a tareas.jsonl (también indexado por chrono).
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path("/data/chrono")
REPO = Path("/opt/arriendo-bot")
SCORER = "http://localhost:8000"
SERVICIOS = ["scorer", "fredy", "vision", "db"]
BOGOTA = timezone(timedelta(hours=-5))
# Horario de Fredy: minutos entre pasadas en horas pico y el resto del día.
INTERVALO_PICO, INTERVALO_NORMAL = 30, 60
MIN_PUBLICACIONES = 30        # con menos datos no se cambia el horario
DISTANCIA_SIMILAR = 3         # bits de SimHash: 0-3 = el mismo texto; 4+ = la misma plantilla con otros datos


def token() -> str:
    for linea in (REPO / ".env").read_text().splitlines():
        if linea.startswith("ARRIENDO_TOKEN="):
            return linea.split("=", 1)[1]
    return ""


def scorer(ruta: str, cuerpo: dict | None = None, timeout: int = 120):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(SCORER + ruta, data=datos, method="POST" if datos is not None else "GET",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        cuerpo = r.read()
    try:
        return json.loads(cuerpo)
    except ValueError:
        return cuerpo


def chrono(*args) -> dict:
    r = subprocess.run(["chrono", *args], cwd=BASE, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[:300]}
    try:
        return json.loads(r.stdout).get("result", {})
    except ValueError:
        return {"error": r.stdout[:300]}


def tarea(nombre: str, nivel: str, mensaje: str) -> None:
    """Deja constancia del resultado de una tarea programada (lo usan también los .sh vía este script)."""
    with open(BASE / "tareas.jsonl", "a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "msg": mensaje, "level": nivel,
                            "service": nombre}, ensure_ascii=False) + "\n")


# 1. Fuentes ------------------------------------------------------------------------------------

NIVEL = re.compile(r"\b(CRITICAL|FATAL|ERROR|WARN(?:ING)?|INFO|DEBUG)\b|Traceback|Exception", re.I)


def exportar_logs() -> int:
    """Registros de los contenedores desde la última vez, en JSONL (sin los chequeos de salud)."""
    estado = BASE / "logs.desde"
    desde = estado.read_text().strip() if estado.exists() else (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    ahora = datetime.now(timezone.utc).isoformat()
    n = 0
    with open(BASE / "logs.jsonl", "a") as out:
        for svc in SERVICIOS:
            r = subprocess.run(["docker", "compose", "logs", "--no-color", "--timestamps", "--since", desde,
                                "--until", ahora, svc], cwd=REPO, capture_output=True, text=True, timeout=120)
            for linea in r.stdout.splitlines():
                m = re.match(r"^\S+\s+\|\s+(\S+)\s(.*)$", linea)
                if not m or "GET /salud" in m.group(2) or not m.group(2).strip():
                    continue
                ts, msg = m.groups()
                hallado = NIVEL.search(msg)
                nivel = (hallado.group(1) or "ERROR").upper() if hallado else "INFO"
                nivel = "WARN" if nivel == "WARNING" else nivel
                out.write(json.dumps({"ts": ts, "msg": msg[:500], "level": nivel, "service": svc}, ensure_ascii=False) + "\n")
                n += 1
    estado.write_text(ahora)
    return n


def descargar(nombre: str) -> int:
    with urllib.request.urlopen(f"{SCORER}/api/{nombre}", timeout=120) as r:
        datos = r.read()
    tmp = BASE / f"{nombre}.nuevo"
    tmp.write_bytes(datos)
    tmp.replace(BASE / nombre)
    return datos.count(b"\n")


def indexar() -> None:
    if not (BASE / ".chrono").exists():
        subprocess.run(["chrono", "init", "historial.jsonl"], cwd=BASE, capture_output=True, timeout=600)
    fuentes = json.dumps(chrono("sources"))
    for f in ("textos.jsonl", "logs.jsonl", "tareas.jsonl"):
        if (BASE / f).exists() and f not in fuentes:
            subprocess.run(["chrono", "add", f], cwd=BASE, capture_output=True, timeout=600)
    subprocess.run(["chrono", "sync"], cwd=BASE, capture_output=True, timeout=600)


# 2. Horario adaptativo ------------------------------------------------------------------------

def horas_pico() -> tuple[list[int], int]:
    """Horas (Bogotá) que suman la mitad de las publicaciones con hora exacta de los últimos 21 días."""
    desde = (datetime.now(timezone.utc) - timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = chrono("timeline", "--level", "publicado", "--bucket", "1h", "--since", desde)
    por_hora = Counter()
    for b in r.get("buckets", r.get("timeline", [])):
        inicio = b.get("at")
        cuenta = b.get("count") or b.get("n") or 0
        if inicio and cuenta:
            hora = datetime.fromisoformat(inicio.replace("Z", "+00:00")).astimezone(BOGOTA).hour
            por_hora[hora] += cuenta
    total, acumulado, pico = sum(por_hora.values()), 0, []
    for hora, n in por_hora.most_common():
        if acumulado >= total / 2:
            break
        pico.append(hora)
        acumulado += n
    return sorted(pico), total


def ajustar_fredy(pico: list[int], total: int) -> dict:
    if total < MIN_PUBLICACIONES or not pico:
        return {"estado": f"faltan datos ({total} publicaciones con hora; se necesitan {MIN_PUBLICACIONES})"}
    hora = datetime.now(BOGOTA).hour
    # Una hora antes del pico ya se acelera, para que la primera pasada caiga dentro.
    intervalo = INTERVALO_PICO if (hora in pico or (hora + 1) % 24 in pico) else INTERVALO_NORMAL
    r = subprocess.run(["docker", "compose", "exec", "-T", "-w", "/fredy", "fredy", "node", "--input-type=module", "-e",
                        "import S from './lib/services/storage/SqliteConnection.js';"
                        "import {upsertSettings, getSettings} from './lib/services/storage/settingsStorage.js';"
                        "await S.init(); const s = await getSettings();"
                        f"if (String(s.interval) !== '{intervalo}') {{ upsertSettings({{interval: '{intervalo}'}}); console.log('cambiado'); }}"],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    if "cambiado" in r.stdout:
        # Fredy guarda los ajustes en memoria y solo los relee cuando él mismo los cambia: se reinicia
        # (unos segundos), lo que además lanza una pasada inmediata al entrar en la hora pico.
        subprocess.run(["docker", "compose", "restart", "fredy"], cwd=REPO, capture_output=True, timeout=180)
        tarea("horario-fredy", "INFO", f"intervalo de Fredy a {intervalo} min (hora {hora}, pico {pico})")
    scorer("/api/experimentos", {"nombre": "horario_adaptativo",
                                 "detalle": {"pico": pico, "pico_min": INTERVALO_PICO, "resto_min": INTERVALO_NORMAL}})
    return {"estado": "activo", "intervalo_ahora": intervalo, "error": r.stderr.strip()[:200] or None}


# 3. Textos casi idénticos ---------------------------------------------------------------------

def buscar_parecidos(horas: int = 2) -> list[dict]:
    desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).strftime("%Y-%m-%dT%H:%M:%SZ")
    nuevos = chrono("events", "--level", "texto", "--since", desde, "--limit", "200").get("events", [])
    pares = []
    for ev in nuevos:
        for otro in chrono("similar", ev["id"]).get("similar", []):
            oid, dist = otro.get("id"), otro.get("distance", otro.get("hamming"))
            if oid and oid != ev["id"] and dist is not None and dist <= DISTANCIA_SIMILAR:
                # El motivo lo decide el scorer, que conoce anunciantes y duplicados entre portales.
                pares.append({"inmueble": ev["id"], "parecido": oid, "distancia": dist})
    if pares:
        scorer("/api/sospechas", {"pares": pares})
    return pares


# 4. Análisis para el mapa y el resumen ---------------------------------------------------------

def analizar(pico: list[int], total_pub: int, horario: dict, pares: list) -> dict:
    hace = lambda d: (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    errores = chrono("top", "level", "--source", f"jsonl:{BASE}/logs.jsonl", "--since", hace(7))
    return {
        "generado": datetime.now(timezone.utc).isoformat(),
        "horas_pico": pico, "publicaciones_con_hora": total_pub, "horario_fredy": horario,
        "barrios_mas_nuevos_7d": chrono("top", "entity", "--level", "nuevo", "--since", hace(7)).get("top", [])[:10],
        "anunciantes_mas_activos_30d": chrono("top", "attr:anunciante", "--level", "nuevo", "--since", hace(30)).get("top", [])[:10],
        "dias_en_mercado_por_barrio": chrono("stats", "dias_en_mercado", "--by", "entity", "--level", "retirado"),
        "demora_deteccion_min": chrono("stats", "demora_min", "--by", "attr:portal", "--level", "nuevo", "--since", hace(14)),
        "bajadas_por_barrio_30d": chrono("top", "entity", "--level", "baja", "--since", hace(30)).get("top", [])[:10],
        "precede_retiro": chrono("precede", "--level", "retirado", "--source", f"jsonl:{BASE}/historial.jsonl"),
        "errores_logs_7d": sum(x.get("count", 0) for x in errores.get("top", []) if x.get("value") in ("ERROR", "FATAL", "CRITICAL")),
        "errores_frecuentes_7d": chrono("patterns", "--level", "ERROR,FATAL,CRITICAL", "--source", f"jsonl:{BASE}/logs.jsonl",
                                        "--since", hace(7), "--limit", "5"),
        "tareas_7d": chrono("top", "level", "--source", f"jsonl:{BASE}/tareas.jsonl", "--since", hace(7)).get("top", []),
        "parecidos_nuevos": len(pares),
    }


def main() -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] == "tarea":  # uso: analisis_chrono.py tarea <nombre> <nivel> <mensaje>
        tarea(*sys.argv[2:5])
        return
    logs = exportar_logs()
    eventos = descargar("historial.jsonl")
    descargar("textos.jsonl")
    indexar()
    pico, total_pub = horas_pico()
    horario = ajustar_fredy(pico, total_pub)
    pares = buscar_parecidos(horas=24 * 3650 if "--todo" in sys.argv else 2)
    analisis = analizar(pico, total_pub, horario, pares)
    tmp = BASE / "analisis.json.nuevo"
    tmp.write_text(json.dumps(analisis, ensure_ascii=False, indent=1, default=str))
    tmp.replace(BASE / "analisis.json")
    ahora = datetime.now(BOGOTA)
    if ahora.hour == 3:
        externas = {"errores_logs": {"": analisis["errores_logs_7d"]},
                    "tareas_fallidas": {"": sum(x.get("count", 0) for x in analisis["tareas_7d"] if x.get("value") == "ERROR")}}
        scorer("/api/metricas/registrar", {"externas": externas})
    if ahora.weekday() == 0 and ahora.hour == 7:
        scorer("/api/resumen/enviar", {})
    print(f"logs +{logs} · historial {eventos} eventos · pico {pico} ({total_pub} con hora) · "
          f"Fredy {horario.get('estado')} · parecidos {len(pares)}")


if __name__ == "__main__":
    main()
