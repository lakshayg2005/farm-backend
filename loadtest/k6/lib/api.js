// Thin HTTP helpers for the Farm API: auth header, JSON, request names for metrics, checks.
import http from 'k6/http';
import { check } from 'k6';

export const BASE = (__ENV.BASE_URL || 'http://localhost/api/v1').replace(/\/$/, '');

// Every load-test account has a phone starting with this prefix, so cleanup can find them all.
// +91 70001xxxxx customers, 70002xxxxx farmers, 70003xxxxx delivery partners.
export const PHONE_PREFIX = '+917000';
export const PASSWORD = __ENV.LOADTEST_PASSWORD || 'LoadTest@2026';

function params(token, name, extra = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (extra.idempotencyKey) headers['Idempotency-Key'] = extra.idempotencyKey;
  const p = { headers, tags: { name }, timeout: extra.timeout || '30s' };
  // k6 treats an explicit `undefined` callback as "classify nothing": only set it when needed
  if (extra.ok) p.responseCallback = http.expectedStatuses(...extra.ok);
  return p;
}

// request('GET', '/catalog/crops', { token, name, body, ok: [200, 404] })
export function request(method, path, opts = {}) {
  const name = opts.name || `${method} ${path.split('?')[0]}`;
  const body = opts.body === undefined ? null : JSON.stringify(opts.body);
  const res = http.request(method, `${BASE}${path}`, body, params(opts.token, name, opts));
  const ok = opts.ok || [200, 201, 202, 204];
  check(res, { [`${name} ok`]: (r) => ok.includes(r.status) }, { name });
  return res;
}

export function json(res, fallback = null) {
  try {
    return res.json();
  } catch (_) {
    return fallback;
  }
}

export function point(lat, lng) {
  return { type: 'Point', coordinates: [lng, lat] };
}

export function pick(list) {
  return list[Math.floor(Math.random() * list.length)];
}

export function jitter(value, spread) {
  return value + (Math.random() * 2 - 1) * spread;
}

export function login(phone, password = PASSWORD) {
  const res = request('POST', '/identity/login', {
    body: { phone, password },
    name: 'POST /identity/login',
  });
  if (res.status !== 200) return null;
  const body = json(res);
  return { access: body.accessToken, refresh: body.refreshToken, user: body.user, at: Date.now() };
}
