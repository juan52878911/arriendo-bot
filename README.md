# arriendo-bot

Radar de arriendos para Bogotá. Revisa los portales a toda hora, cruza cada anuncio con datos
abiertos de la ciudad (seguridad, estratos, TransMilenio), le pone un puntaje de 0 a 100 según
`config/config.yaml` y avisa por Telegram los que valen la pena. No usa IA para funcionar.

```
Fredy (captura Fincaraíz) ──raw──► scorer ──► PostGIS ◄── ingesta (datos abiertos)
        │                            │
        └──webhook (enlace) ─────────┤──► Telegram
                                     └──► mapa y ranking  http://<servidor>:8000
```

| Pieza | Qué hace |
|---|---|
| [Fredy](https://github.com/orangecoding/fredy) | Programa las búsquedas, deduplica, guarda historial y tiene su propia interfaz (`:9998`). Se construye desde su código, fijado a un commit, con el portal `fredy/fincaraiz.js`. |
| `scorer/` | Recibe cada anuncio, lo ubica en su sector catastral, aplica los filtros duros, calcula el tiempo al destino con [Transitous](https://transitous.org), puntúa y avisa. Sirve el mapa. |
| `config/fuentes.yaml` | Datos abiertos que se descargan (cada 30 a 90 días) y cargan en PostGIS. |
| `respaldo` | Copia diaria de la base al NAS (`/data/respaldos`), 14 días. |

## Puesta en marcha (servidor)

```bash
cp .env.example .env        # rellenar contraseñas, token y Telegram
sh fredy/build.sh           # descarga Fredy fijado y le añade Fincaraíz
docker compose up -d --build
```

Luego se configura Fredy sin tocar su interfaz: canal HTTP hacia el scorer, un job por barrio
(3+ habitaciones, hasta $3.000.000, más recientes primero), pasada cada 60 minutos y sin telemetría.
Para cambiar barrios, edita la lista de `fredy/configurar.mjs` y vuelve a correrlo:

```bash
docker compose cp fredy/configurar.mjs fredy:/fredy/configurar.mjs
docker compose exec -w /fredy fredy node configurar.mjs
docker compose restart fredy
```

La interfaz de Fredy (`http://<servidor>:9998`) arranca con `admin` / `admin`: cámbiala en
*Usuarios* la primera vez que entres.

## Telegram

1. Habla con [@BotFather](https://t.me/BotFather), crea un bot y copia el token en `TELEGRAM_BOT_TOKEN`.
2. Escríbele cualquier cosa al bot y abre `https://api.telegram.org/bot<token>/getUpdates`:
   el número en `chat.id` va en `TELEGRAM_CHAT_ID`.
3. `docker compose up -d scorer`.

Sin token, los avisos solo quedan en el log del scorer.

## Ajustar el puntaje

`config/config.yaml` se relee en cada inmueble. Tras cambiar pesos o zonas, para repuntuar todo:

```bash
curl -X POST -H "Authorization: Bearer $ARRIENDO_TOKEN" http://localhost:8000/api/recalcular
```

## Datos y sus límites

| Capa | Fuente | Granularidad |
|---|---|---|
| Seguridad | Secretaría de Seguridad: incidentes reportados (hurto, riñas, disparos, porte de armas) | Sector catastral, enero–agosto de cada año 2018–2026 |
| Delitos | Secretaría de Seguridad: delitos de alto impacto | Localidad |
| Estrato | Secretaría de Planeación: estrato por lote | Se resume por sector catastral |
| TransMilenio | TransMilenio S.A.: estaciones troncales | Puntos |
| Tiempo al destino | Transitous (GTFS de TransMilenio y SITP) | Por inmueble, salida 7:30 de un día hábil |

- La seguridad mide incidentes por km², no por habitante: un sector comercial muy concurrido sale peor
  que uno residencial con la misma inseguridad para quien vive allí.
- Los datos de seguridad no son puntos exactos. El detalle por dirección se pide a la Policía Nacional
  con un derecho de petición.
- El puntaje no reemplaza la visita: la cocina, el ruido real y el certificado de tradición y libertad
  se revisan en persona, y no se paga reserva antes de eso.

## Licencias

Fredy se distribuye bajo Apache-2.0 con Commons Clause: uso personal sí, venderlo no. Los datos
abiertos de Bogotá son CC BY / CC BY-SA: si publicas algo hecho con ellos, cita la fuente.
