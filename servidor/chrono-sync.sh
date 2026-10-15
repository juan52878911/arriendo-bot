#!/bin/sh
# arriendo-bot: cada día baja el historial del mercado y lo indexa con chrono en el NAS (/data/chrono).
# Consultas desde el servidor:  cd /data/chrono && chrono top entity · chrono events --level baja
# Va en /etc/cron.daily/ del LXC; chrono se instala con el install.sh de su repositorio.
set -eu
cd /data/chrono
curl -fsS localhost:8000/api/historial.jsonl -o historial.jsonl.nuevo
mv historial.jsonl.nuevo historial.jsonl
if [ -d .chrono ]; then chrono sync; else chrono init historial.jsonl; fi
