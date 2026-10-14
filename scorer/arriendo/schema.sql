-- Esquema del scorer. Idempotente: se aplica en cada arranque.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Capas de datos abiertos -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS localidad (
  codigo        text PRIMARY KEY,
  nombre        text NOT NULL,
  geom          geometry(MultiPolygon, 4326) NOT NULL,
  hurto_personas_var real,
  atributos     jsonb
);
CREATE INDEX IF NOT EXISTS localidad_geom ON localidad USING gist (geom);

CREATE TABLE IF NOT EXISTS sector (
  codigo        text PRIMARY KEY,           -- sector catastral, 6 dígitos
  nombre        text NOT NULL,
  geom          geometry(MultiPolygon, 4326) NOT NULL,
  area_km2      real,
  localidad     text,
  periodo       text,                       -- p. ej. "Ene-Ago (2025vs2026)"
  incidentes    jsonb,                      -- {"hurto": {"2025": 130, "2026": 97}, ...}
  densidad      real,                       -- incidentes graves del último año por km²
  seguridad_pct real,                       -- percentil de densidad en Bogotá (0 = el más tranquilo)
  estrato_moda  int,
  estratos      jsonb                       -- {"3": 812, "4": 40}
);
CREATE INDEX IF NOT EXISTS sector_geom ON sector USING gist (geom);

CREATE TABLE IF NOT EXISTS estacion_tm (
  id      text PRIMARY KEY,
  nombre  text,
  geom    geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS estacion_tm_geom ON estacion_tm USING gist (geom);

-- OpenStreetMap: vías principales, parques, mercados y ciclorrutas.
CREATE TABLE IF NOT EXISTS via (
  id      text PRIMARY KEY,
  tipo    text,                              -- motorway | trunk | primary
  nombre  text,
  geom    geometry(LineString, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS via_geom ON via USING gist (geom);

CREATE TABLE IF NOT EXISTS parque (
  id       text PRIMARY KEY,
  nombre   text,
  area_m2  real,
  geom     geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS parque_geom ON parque USING gist (geom);

CREATE TABLE IF NOT EXISTS poi (
  id      text PRIMARY KEY,
  tipo    text,                              -- supermercado | plaza_mercado
  nombre  text,
  geom    geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS poi_geom ON poi USING gist (geom);

CREATE TABLE IF NOT EXISTS ciclorruta (
  id    text PRIMARY KEY,
  geom  geometry(LineString, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS ciclorruta_geom ON ciclorruta USING gist (geom);

CREATE TABLE IF NOT EXISTS fuente (
  id            text PRIMARY KEY,
  actualizada   timestamptz NOT NULL,
  filas         int NOT NULL,
  nota          text
);

-- Inmuebles -------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS inmueble (
  id             text PRIMARY KEY,           -- "<portal>:<id del portal>"
  portal         text NOT NULL,
  url            text NOT NULL,
  titulo         text,
  descripcion    text,
  canon          bigint,
  total          bigint,                     -- canon + administración
  administracion bigint,
  area           real,
  habitaciones   int,
  banos          int,
  estrato        int,
  piso           int,
  parqueaderos   int,
  facilidades    text[],
  imagenes       text[],
  publicado      date,
  geom           geometry(Point, 4326),
  barrio_portal  text,
  sector         text REFERENCES sector (codigo),
  localidad      text,
  tm_m           real,                       -- metros a la estación troncal más cercana
  tiempo_min     int,
  tiempo_fuente  text,                       -- 'transitous' | 'estimado'
  fredy_url      text,
  puntaje        real,
  detalle        jsonb,
  descartes      text[],
  alertas        text[],
  avisado        timestamptz,
  visto_primero  timestamptz NOT NULL DEFAULT now(),
  visto_ultimo   timestamptz NOT NULL DEFAULT now(),
  crudo          jsonb
);
CREATE INDEX IF NOT EXISTS inmueble_geom ON inmueble USING gist (geom);
CREATE INDEX IF NOT EXISTS inmueble_puntaje ON inmueble (puntaje DESC);

ALTER TABLE inmueble ADD COLUMN IF NOT EXISTS duplicado_de text;
ALTER TABLE inmueble ADD COLUMN IF NOT EXISTS externos jsonb;   -- consultas por inmueble: suelo, siniestros

-- Historial de precios: lo que después se lleva a chrono.
CREATE TABLE IF NOT EXISTS precio (
  inmueble  text NOT NULL REFERENCES inmueble (id) ON DELETE CASCADE,
  visto     timestamptz NOT NULL DEFAULT now(),
  total     bigint NOT NULL,
  PRIMARY KEY (inmueble, visto)
);
