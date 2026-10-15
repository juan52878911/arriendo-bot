/*
 * Copyright (c) 2026 JuanPBedoya (arriendo-bot).
 * Portal Metrocuadrado para Fredy (https://github.com/orangecoding/fredy), de Christian Kellner. Se compila
 * dentro de Fredy, así que se distribuye con su misma licencia: Apache-2.0 con Commons Clause y
 * cláusula de atribución.
 */

/**
 * Metrocuadrado, read from the results its search page streams to the browser.
 *
 * The page is a Next.js app-router page: the first fifty results travel inside the React flight
 * payload (`self.__next_f.push([1, "…"])`) under `initialResults.results`, with rent, fee, area,
 * rooms, bathrooms, stratum, coordinates, gallery and a list of tagged features ("tipoEstufa:Gas",
 * "ascensor:S"). One request per run; the path takes the room filter
 * (`/apartamentos/arriendo/bogota/<barrio>/3-habitaciones/`), which keeps a neighbourhood well under
 * fifty.
 *
 * As with Fincaraíz, the raw records are handed to the arriendo-bot scorer when `ARRIENDO_RAW_URL`
 * is set, since Fredy's listing model has no room for bathrooms, stratum or the fee.
 */

import { buildHash, isOneOf } from '../utils.js';
import logger from '../services/logger.js';
/** @import { ParsedListing } from '../types/listing.js' */
/** @import { ProviderConfig } from '../types/providerConfig.js' */

const BASE_URL = 'https://www.metrocuadrado.com';
const MEDIA_URL = 'https://multimedia.metrocuadrado.com';

const REQUEST_HEADERS = {
  Accept: 'text/html,application/xhtml+xml',
  'Accept-Language': 'es-CO,es;q=0.9',
  'User-Agent':
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
};

/**
 * fetch con un reintento a los 5 s si la red falla (no si el portal responde con error). chrono encontró
 * en los registros fallos de DNS esporádicos (EAI_AGAIN) que hacían perder la pasada entera de un portal.
 *
 * @param {string} url
 * @param {RequestInit} opciones
 * @returns {Promise<Response>}
 */
async function fetchConReintento(url, opciones) {
  try {
    return await fetch(url, opciones);
  } catch (err) {
    logger.warn(`Red falló en ${url} (${err.cause?.code ?? err.message}); reintento en 5 s`);
    await new Promise((resolve) => setTimeout(resolve, 5000));
    return fetch(url, opciones);
  }
}


const FLIGHT_CHUNK = /self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)/g;

/**
 * The `initialResults` object of a search page, or null when the page does not carry one.
 *
 * @param {string} html
 * @returns {{totalHits: number, results: Object[]}|null}
 */
export function parseSearchPage(html) {
  let flight = '';
  for (const match of html.matchAll(FLIGHT_CHUNK)) {
    flight += JSON.parse(`"${match[1]}"`);
  }
  const key = '"initialResults":';
  const at = flight.indexOf(key);
  if (at < 0) {
    return null;
  }
  // The object is followed by more flight data; walk braces to find where it ends.
  const start = at + key.length;
  let depth = 0;
  let inString = false;
  for (let i = start; i < flight.length; i++) {
    const c = flight[i];
    if (inString) {
      if (c === '\\') i++;
      else if (c === '"') inString = false;
    } else if (c === '"') inString = true;
    else if (c === '{') depth++;
    else if (c === '}' && --depth === 0) {
      const result = JSON.parse(flight.slice(start, i + 1));
      return Array.isArray(result?.results) ? result : null;
    }
  }
  return null;
}

function shareRaw(records, searchUrl) {
  const target = process.env.ARRIENDO_RAW_URL;
  if (!target || records.length === 0) {
    return;
  }
  const headers = { 'Content-Type': 'application/json' };
  if (process.env.ARRIENDO_TOKEN) {
    headers.Authorization = `Bearer ${process.env.ARRIENDO_TOKEN}`;
  }
  fetch(target, {
    method: 'POST',
    headers,
    body: JSON.stringify({ portal: 'metrocuadrado', searchUrl, records }),
  }).catch((err) => logger.warn(`Could not hand Metrocuadrado records to the scorer: ${err.message}`));
}

/**
 * @param {string} url
 * @returns {Promise<Object[]>}
 */
async function getListings(url) {
  const response = await fetchConReintento(url, { headers: REQUEST_HEADERS });
  if (!response.ok) {
    logger.error(`Error fetching Metrocuadrado: ${response.status} ${response.statusText}`);
    return [];
  }
  const page = parseSearchPage(await response.text());
  if (page == null) {
    logger.error('Metrocuadrado page carried no results; the page layout may have changed');
    return [];
  }
  // Listing codes grow with time (…-M7128835 is newer than …-M6947821): newest first.
  const records = page.results
    .filter((r) => r?.midinmueble)
    .sort((a, b) => codeNumber(b.midinmueble) - codeNumber(a.midinmueble));
  shareRaw(records, url);
  return records;
}

function codeNumber(code) {
  const digits = /M(\d+)$/.exec(String(code));
  return digits ? Number(digits[1]) : 0;
}

function toNumber(value) {
  if (value == null || value === '') return null;
  const n = Number(String(value).replace(/[^\d.]/g, ''));
  return Number.isFinite(n) && n > 0 ? n : null;
}

/**
 * @param {any} o
 * @returns {ParsedListing}
 */
export function normalize(o) {
  const rent = toNumber(o.mvalorarriendo);
  const fee = toNumber(o.data?.mvaloradministracion);
  const total = rent != null ? rent + (fee ?? 0) : null;
  const gallery = (o.mgaleriainmueble ?? []).slice(0, 20).map((name) => ({ url: `${MEDIA_URL}/${o.midinmueble}/${name}_p.jpg` }));
  return {
    id: buildHash(String(o.midinmueble), String(total)),
    title: o.title,
    link: o.link ? `${BASE_URL}${o.link}` : null,
    price: total,
    chargesIncluded: fee != null ? true : undefined,
    charges: fee ?? undefined,
    size: toNumber(o.areaprivada) ?? toNumber(o.marea) ?? toNumber(o.mareac),
    rooms: toNumber(o.mnrocuartos),
    address: o.mnombrecomunbarrio ?? o.mbarrio ?? null,
    description: o.comment,
    image: o.imageLink ?? null,
    images: gallery,
    latitude: typeof o.localizacion?.lat === 'number' ? o.localizacion.lat : undefined,
    longitude: typeof o.localizacion?.lon === 'number' ? o.localizacion.lon : undefined,
  };
}

function applyBlacklist(o, appliedBlackList) {
  return o.title != null && !isOneOf(o.title, appliedBlackList) && !isOneOf(o.description, appliedBlackList);
}

/** @type {ProviderConfig} */
const config = {
  url: null,
  requiredFieldNames: ['id', 'title', 'link', 'price', 'address'],
  crawlContainer: null,
  crawlFields: {},
  sortByDateParam: null,
  getListings,
  normalize,
};

export const metaInformation = {
  countries: ['co'],
  name: 'Metrocuadrado',
  baseUrl: `${BASE_URL}/`,
  hosts: ['www.metrocuadrado.com', 'metrocuadrado.com'],
  id: 'metrocuadrado',
};

export const createConfig = (sourceConfig, blacklist = []) => ({
  ...config,
  enabled: sourceConfig.enabled,
  url: sourceConfig.url,
  filter: (listing) => applyBlacklist(listing, blacklist ?? []),
});

export { config };
