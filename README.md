# arriendo-bot

Radar de arriendos para Bogotá. Revisa los portales a toda hora, cruza cada anuncio con datos
abiertos de la ciudad (seguridad, estratos, TransMilenio), le pone un puntaje de 0 a 100 según
`config/config.yaml` y avisa por Telegram los que valen la pena. No usa IA para funcionar.

```
Fredy (Fincaraíz, Metrocuadrado, Ciencuadras) ──raw──► scorer ──► PostGIS ◄── ingesta (datos abiertos, OSM)
        │                                                │  ▲
        └──webhook (enlace) ─────────────────────────────┤  └── vision (SigLIP + cabezas, fotos)
                                                         ├──► Telegram
                                                         ├──► mapa y ranking   http://<servidor>:8000
                                                         ├──► etiquetar fotos  http://<servidor>:8000/etiquetar
                                                         └──► historial.jsonl ──► chrono
```

| Pieza | Qué hace |
|---|---|
| [Fredy](https://github.com/orangecoding/fredy) | Programa las búsquedas, deduplica, guarda historial y tiene su propia interfaz (`:9998`). Se construye desde su código, fijado a un commit, con los portales `fredy/fincaraiz.js`, `fredy/metrocuadrado.js` y `fredy/ciencuadras.js`. |
| `scorer/` | Recibe cada anuncio, lo ubica en su sector catastral, aplica los filtros duros, calcula el tiempo al destino con [Transitous](https://transitous.org), puntúa y avisa. Sirve el mapa. |
| `config/fuentes.yaml` | Datos abiertos que se descargan (cada 30 a 90 días) y cargan en PostGIS. |
| `vision/` | SigLIP calcula un vector por foto; cabezas pequeñas (`config/vision.yaml`) clasifican espacio, horno, estufa, extractor, piso, ducha, luz y renders. Solo analiza fotos de candidatos cerca del umbral. |
| `chrono/sync.sh` | Baja el historial (nuevo, bajas, subidas, retirados por barrio) y lo indexa con chrono. |
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

## Que nada falle en silencio

- Cada hora, el scorer revisa que cada portal haya traído anuncios en las últimas 24 h, que las
  fuentes de datos estén al día y que el servicio de visión responda. Si algo falla, avisa por
  Telegram (una vez al día por problema) y lo muestra en una franja arriba del mapa
  (`/api/estado`).
- Un anuncio que deja de verse en los listados no se da por retirado: se abre su página (hasta 30
  por hora, con pausa) y solo se marca si el portal ya no lo tiene.
- Respaldos diarios en el NAS: PostGIS (`arriendo-*.dump`) y el SQLite de Fredy (`fredy-*.db`). Cada
  semana `servidor/probar-respaldos.sh` restaura el último en una base temporal y avisa si falla.
- Cada semana `servidor/probar-portales.sh` abre una búsqueda real en cada portal (las pruebas de
  `fredy/tests/`) y avisa si alguno cambió su página.

## Correos de alerta (respaldo)

Guarda búsquedas en los portales con alertas por correo y pon `IMAP_USER` / `IMAP_PASSWORD` (en
Gmail, una contraseña de aplicación) en `.env`. Cada 30 minutos se leen los correos de los portales
de los últimos 7 días, se sacan los enlaces a anuncios y los que el sistema no conocía se abren y
entran al mismo flujo. Solo lectura: no marca ni borra correos.

## Dónde se guarda cada cosa

| Qué | Dónde | Por qué |
|---|---|---|
| Fotos (una por foto real, WebP ≤1024 px) | NAS `/data/fotos/<aa>/<huella>.webp` | Lo único que puede crecer de verdad |
| Modelo de visión (~780 MB) y cabezas entrenadas | NAS `/data/modelos` | Pesado y fuera de la imagen de Docker |
| Respaldos diarios (PostGIS y Fredy, 14 días) | NAS `/data/respaldos` | |
| PostGIS y SQLite de Fredy | Disco local del LXC | Las bases de datos sobre NFS se corrompen con los bloqueos; sus respaldos ya van al NAS |
| Imágenes de Docker | Disco local del LXC | Docker no funciona sobre NFS; limpieza semanal en `servidor/docker-limpieza` |

**Fotos sin duplicados.** Cada foto descargada recibe una huella visual (dHash de 64 bits) que no
cambia al redimensionarla ni recomprimirla. Si una foto nueva difiere de otra ya guardada en 6 bits
o menos y su contenido (vector SigLIP) coincide en un 95 % o más, es la misma foto: se guarda un solo
archivo (el de mayor resolución), cada URL de cada portal apunta a él y las etiquetas van a la foto,
no a la URL. Las URLs ya vistas no se vuelven a descargar. Las fotos sin etiquetar de anuncios
retirados hace más de 180 días se borran del NAS; las etiquetadas se quedan como material de
entrenamiento.

## Gráfica integrada

La visión usa la Intel UHD 630 de `pve` con OpenVINO en media precisión (f16): 12 fotos en 0,85 s
frente a 1,69 s en CPU, con los mismos vectores (similitud ≥ 0,9999). Requiere pasar el dispositivo
al LXC (`pct set 107 --dev0 /dev/dri/renderD128,mode=0666`) y lo usa el contenedor `vision`
(`devices` en `docker-compose.yml`). Sin gráfica, o con `VISION_DISPOSITIVO=cpu`, sigue en CPU.

## Enseñarle a ver las fotos

Las cabezas empiezan "zero-shot" (comparan la foto con frases). En `http://<servidor>:8000/etiquetar`
eliges un criterio y respondes con las teclas 1-9; primero salen las fotos donde el modelo más duda.
Con unas 20-30 por clase, **Entrenar** ajusta una regresión logística por cabeza y la usa solo si
acierta más que el zero-shot en validación cruzada; luego reclasifica todas las fotos guardadas y
repuntúa, sin volver a descargarlas.

## Historial con chrono

El servidor lo indexa cada día (`servidor/chrono-sync.sh`, índice en el NAS `/data/chrono`):

```bash
ssh arriendo 'cd /data/chrono && chrono top entity'          # barrios con más movimiento
ssh arriendo 'cd /data/chrono && chrono events --level baja' # bajadas de precio
```

Para tenerlo en tu Mac: `sh chrono/sync.sh`.

## Uso responsable de los portales

Cada búsqueda es una sola petición por hora (Ciencuadras rota 10 barrios por pasada), con pausas de
varios segundos entre páginas, sin navegador automatizado ni evasión de bloqueos. Las fotos y el
detalle se piden solo para los candidatos que pasan los filtros. Es una herramienta de búsqueda
personal: si la adaptas, mantén ese ritmo y respeta los términos de cada portal.

## Datos y sus límites

| Capa | Fuente | Granularidad |
|---|---|---|
| Seguridad | Secretaría de Seguridad: incidentes reportados (hurto, riñas, disparos, porte de armas) | Sector catastral, enero–agosto de cada año 2018–2026 |
| Delitos | Secretaría de Seguridad: delitos de alto impacto | Localidad |
| Estrato | Secretaría de Planeación: estrato por lote | Se resume por sector catastral |
| TransMilenio | TransMilenio S.A.: estaciones troncales | Puntos |
| Tiempo al destino | Transitous (GTFS de TransMilenio y SITP) | Por inmueble, salida 7:30 de un día hábil |
| Aire | Secretaría de Ambiente: PM2.5 promedio anual 2025 (RMCAB interpolada) | 8 franjas de concentración |
| Ruido nocturno | Secretaría de Ambiente: Mapa Estratégico de Ruido 2018-2021, noche, día ordinario | Por inmueble, en dB(A) |
| Vías principales | OpenStreetMap | Distancia por inmueble |
| Zona de 45 min al destino | Transitous, llegada 8:15 | Polígono en el mapa (se recalcula cada 30 días) |
| Entorno | OpenStreetMap: parques de 2 ha o más, supermercados y plazas, ciclorrutas | Por inmueble |
| Valor del suelo | Catastro: mediana del m² de terreno por manzana, 2026 | Por inmueble (informativo) |
| Siniestros viales | Secretaría de Movilidad, histórico 2015-2021 | A menos de 150 m (alerta) |

- Las coordenadas vienen de los portales y a veces son aproximadas: "a 30 m de una avenida" es una
  alerta para revisar en la visita, no un descarte.
- La seguridad mide incidentes por km², no por habitante: un sector comercial muy concurrido sale peor
  que uno residencial con la misma inseguridad para quien vive allí.
- Los datos de seguridad no son puntos exactos. El detalle por dirección se pide a la Policía Nacional
  con un derecho de petición.
- El puntaje no reemplaza la visita: la cocina, el ruido real y el certificado de tradición y libertad
  se revisan en persona, y no se paga reserva antes de eso.

## Licencias

Este repositorio es [MIT](LICENSE), con una excepción: los portales `fredy/fincaraiz.js`,
`fredy/metrocuadrado.js` y `fredy/ciencuadras.js` se compilan dentro de Fredy y llevan su licencia
(Apache-2.0 con Commons Clause: uso personal sí, venderlo no). Los datos
abiertos de Bogotá son CC BY / CC BY-SA: si publicas algo hecho con ellos, cita la fuente.
