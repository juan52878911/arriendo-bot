/*
 * Copyright (c) 2026 by Christian Kellner.
 * Licensed under Apache-2.0 with Commons Clause and Attribution/Naming Clause
 */

/**
 * Ciencuadras, read from the state its Angular page transfers to the browser.
 *
 * The server-rendered search page carries `<script id="result-state">` with the first twenty
 * results under the key `results-<path>`, entity-escaped (`&q;` for a quote). Each record has rent
 * and administration fee as separate numbers, area, rooms, bathrooms, stratum, coordinates and an
 * exact `createdAt`. The server ignores filter parameters - only the path counts - so a job points
 * at one neighbourhood (`/arriendo/apartamento/bogota/<barrio>`), which is short enough for a page.
 *
 * A locality has more listings than one page holds and the server ignores pagination too, so a job
 * can list neighbourhoods instead: `…/bogota/teusaquillo?barrios=armenia,palermo,…`. Each run reads
 * `CIENCUADRAS_POR_PASADA` of them (default 10), rotating with the hour so that every neighbourhood is
 * read every few hours. The rotation comes from the clock, not from module state: providers are
 * stateless (two runs of a job can overlap).
 *
 * Raw records go to the arriendo-bot scorer when `ARRIENDO_RAW_URL` is set.
 */

import { buildHash, isOneOf } from '../utils.js';
import logger from '../services/logger.js';
import { publicationDate } from '../utils/publicationDate.js';
/** @import { ParsedListing } from '../types/listing.js' */
/** @import { ProviderConfig } from '../types/providerConfig.js' */

const BASE_URL = 'https://www.ciencuadras.com';

const REQUEST_HEADERS = {
  Accept: 'text/html,application/xhtml+xml',
  'Accept-Language': 'es-CO,es;q=0.9',
  'User-Agent':
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
};

const STATE = /<script id="result-state" type="application\/json">([\s\S]*?)<\/script>/;
const ENTITIES = { '&q;': '"', '&s;': "'", '&a;': '&', '&l;': '<', '&g;': '>' };

/**
 * @param {string} html
 * @returns {Object[]|null}
 */
export function parseSearchPage(html) {
  const match = STATE.exec(html);
  if (match == null) {
    return null;
  }
  const state = JSON.parse(match[1].replace(/&[qsalg];/g, (e) => ENTITIES[e]));
  const key = Object.keys(state).find((k) => k.startsWith('results-'));
  const results = key ? state[key]?.data?.results : null;
  return Array.isArray(results) ? results : null;
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
    body: JSON.stringify({ portal: 'ciencuadras', searchUrl, records }),
  }).catch((err) => logger.warn(`Could not hand Ciencuadras records to the scorer: ${err.message}`));
}

const PER_RUN = Number(process.env.CIENCUADRAS_POR_PASADA ?? 10);
const PAGE_DELAY_MS = 3000;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * The search pages one run reads: the URL itself, or this hour's share of its `barrios` list.
 *
 * @param {string} url
 * @param {Date} [now]
 * @returns {string[]}
 */
export function pagesForRun(url, now = new Date()) {
  const parsed = new URL(url);
  const barrios = (parsed.searchParams.get('barrios') ?? '').split(',').map((b) => b.trim()).filter(Boolean);
  if (barrios.length === 0) {
    return [url];
  }
  const turns = Math.ceil(barrios.length / PER_RUN);
  const turn = Math.floor(now.getTime() / 3_600_000) % turns;
  return barrios
    .filter((_, i) => i % turns === turn)
    .map((b) => `${BASE_URL}/arriendo/apartamento/bogota/${encodeURIComponent(b)}`);
}

/**
 * @param {string} url
 * @returns {Promise<Object[]>}
 */
async function getListings(url) {
  const results = [];
  const pages = pagesForRun(url);
  for (const [n, page] of pages.entries()) {
    if (n > 0) {
      await sleep(PAGE_DELAY_MS);
    }
    const response = await fetch(page, { headers: REQUEST_HEADERS });
    if (!response.ok) {
      logger.error(`Error fetching Ciencuadras ${page}: ${response.status} ${response.statusText}`);
      continue;
    }
    const parsed = parseSearchPage(await response.text());
    if (parsed == null) {
      logger.error(`Ciencuadras page ${page} carried no results; the page layout may have changed`);
      continue;
    }
    results.push(...parsed);
  }
  const seen = new Map(results.filter((r) => r?.id != null).map((r) => [r.id, r]));
  const records = [...seen.values()]
    .sort((a, b) => String(b.createdAt ?? '').localeCompare(String(a.createdAt ?? '')));
  shareRaw(records, url);
  return records;
}

function toNumber(value) {
  if (value == null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/**
 * @param {any} o
 * @returns {ParsedListing}
 */
export function normalize(o) {
  const rent = toNumber(o.rentPrice);
  const fee = toNumber(o.adminValue);
  const total = rent != null ? rent + (fee ?? 0) : null;
  return {
    id: buildHash(String(o.id), String(total)),
    title: `Apartamento en arriendo en ${o.neighborhood ?? o.locality ?? 'Bogotá'}`,
    link: o.url ? `${BASE_URL}${o.url}` : null,
    price: total,
    chargesIncluded: fee != null ? true : undefined,
    charges: fee ?? undefined,
    size: toNumber(o.privateArea) ?? toNumber(o.area),
    rooms: toNumber(o.rooms),
    address: o.address ?? o.neighborhood ?? null,
    publishedAt: publicationDate(o.createdAt),
    image: o.image ?? null,
    latitude: typeof o.coordinates?.latitude === 'number' ? o.coordinates.latitude : undefined,
    longitude: typeof o.coordinates?.longitude === 'number' ? o.coordinates.longitude : undefined,
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
  name: 'Ciencuadras',
  baseUrl: `${BASE_URL}/`,
  hosts: ['www.ciencuadras.com', 'ciencuadras.com'],
  id: 'ciencuadras',
};

export const createConfig = (sourceConfig, blacklist = []) => ({
  ...config,
  enabled: sourceConfig.enabled,
  url: sourceConfig.url,
  filter: (listing) => applyBlacklist(listing, blacklist ?? []),
});

export { config };
