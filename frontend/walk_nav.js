/**
 * Live pedestrian assist: GPS watch + route polyline + optional compass for left/right.
 * Uses /api/maps/routes/compute (WALK). Not an LLM loop — deterministic cues + TTS.
 *
 * Limits: phone compass is noisy; sealed buildings weaken GPS; browser APIs vary (esp. iOS).
 */

import * as speechBus from './speech_bus.js';

const R_EARTH_M = 6371000;
const OFF_ROUTE_M = 48;
const OFF_ROUTE_REPEAT_MS = 22_000;
const STEP_REACHED_M = 24;
const NEAR_DEST_M = 52;
const ANNOUNCE_THROTTLE_MS = 6500;
const WRONG_DIR_HOLD_MS = 10_000;

/** @type {number | null} */
let _watchId = null;
/** @type {((e: Event) => void) | null} */
let _orientHandler = null;
let _state = /** @type {null | { poly: { lat: number; lng: number }[], steps: WalkStep[], destLat: number, destLng: number, stepIndex: number, lastAnnounce: number, lastPhrase: string, offRouteSince: number | null, nearSpoken: boolean, wrongSpokenAt: number, headingSmooth: number | null }} */ (null);

/**
 * @typedef {{ instr: string, distText: string, endLat: number | null, endLng: number | null }} WalkStep
 */

function distMeters(a, b) {
  const φ1 = (a.lat * Math.PI) / 180;
  const φ2 = (b.lat * Math.PI) / 180;
  const Δφ = ((b.lat - a.lat) * Math.PI) / 180;
  const Δλ = ((b.lng - a.lng) * Math.PI) / 180;
  const s =
    Math.sin(Δφ / 2) * Math.sin(Δφ / 2) +
    Math.cos(φ1) * Math.cos(φ2) * Math.sin(Δλ / 2) * Math.sin(Δλ / 2);
  return 2 * R_EARTH_M * Math.asin(Math.min(1, Math.sqrt(s)));
}

/** Closest distance from point p to segment a–b (meters, planar approx near p). */
function distancePointToSegmentMeters(p, a, b) {
  const cos = Math.cos((p.lat * Math.PI) / 180);
  const ax = ((a.lng - p.lng) * 111320 * cos);
  const ay = (a.lat - p.lat) * 110540;
  const bx = ((b.lng - p.lng) * 111320 * cos);
  const by = (b.lat - p.lat) * 110540;
  const abx = bx - ax;
  const aby = by - ay;
  let t = (-(ax * abx + ay * aby)) / (abx * abx + aby * aby + 1e-9);
  t = Math.max(0, Math.min(1, t));
  const cx = ax + t * abx;
  const cy = ay + t * aby;
  return Math.sqrt(cx * cx + cy * cy);
}

function decodePolyline(encoded) {
  if (!encoded || typeof encoded !== 'string') return [];
  const points = [];
  let index = 0;
  let lat = 0;
  let lng = 0;
  while (index < encoded.length) {
    let b;
    let shift = 0;
    let result = 0;
    do {
      b = encoded.charCodeAt(index++) - 63;
      result |= (b & 0x1f) << shift;
      shift += 5;
    } while (b >= 0x20);
    const dlat = result & 1 ? ~(result >> 1) : result >> 1;
    lat += dlat;
    shift = 0;
    result = 0;
    do {
      b = encoded.charCodeAt(index++) - 63;
      result |= (b & 0x1f) << shift;
      shift += 5;
    } while (b >= 0x20);
    const dlng = result & 1 ? ~(result >> 1) : result >> 1;
    lng += dlng;
    points.push({ lat: lat / 1e5, lng: lng / 1e5 });
  }
  return points;
}

function minDistanceToPolylineMeters(p, poly) {
  if (!poly.length) return Infinity;
  let m = Infinity;
  for (let i = 0; i < poly.length - 1; i++) {
    const d = distancePointToSegmentMeters(p, poly[i], poly[i + 1]);
    if (d < m) m = d;
  }
  return m;
}

/** Bearing from a → b in degrees, 0 = north, clockwise. */
function bearingDegrees(a, b) {
  const φ1 = (a.lat * Math.PI) / 180;
  const φ2 = (b.lat * Math.PI) / 180;
  const Δλ = ((b.lng - a.lng) * Math.PI) / 180;
  const y = Math.sin(Δλ) * Math.cos(φ2);
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  const θ = (Math.atan2(y, x) * 180) / Math.PI;
  return (θ + 360) % 360;
}

/** Normalize delta to (-180, 180]. */
function relAngleDeg(fromDeg, toDeg) {
  let d = (toDeg - fromDeg + 360) % 360;
  if (d > 180) d -= 360;
  return d;
}

/**
 * How target sits relative to user's facing (heading: deg clockwise from north).
 * @returns {'left'|'right'|'ahead'|'behind'|'unknown'}
 */
function sideRelativeToFacing(headingDeg, bearingToTargetDeg) {
  if (headingDeg == null || Number.isNaN(headingDeg)) return 'unknown';
  const r = relAngleDeg(headingDeg, bearingToTargetDeg);
  if (Math.abs(r) <= 40) return 'ahead';
  if (r > 40 && r <= 130) return 'right';
  if (r < -40 && r >= -130) return 'left';
  return 'behind';
}

function extractLatLng(loc) {
  if (!loc || typeof loc !== 'object') return { lat: null, lng: null };
  const ll = loc.latLng || loc;
  const lat = ll.latitude ?? ll.lat;
  const lng = ll.longitude ?? ll.lng;
  if (typeof lat === 'number' && typeof lng === 'number') return { lat, lng };
  return { lat: null, lng: null };
}

/**
 * @param {Record<string, unknown>} raw
 * @returns {{ poly: { lat: number; lng: number }[], steps: WalkStep[], destLat: number | null, destLng: number | null } | null}
 */
function parseRouteRaw(raw) {
  const routes = raw?.routes;
  if (!Array.isArray(routes) || !routes.length) return null;
  const r0 = routes[0];
  const enc = r0?.polyline?.encodedPolyline;
  const poly = typeof enc === 'string' ? decodePolyline(enc) : [];
  /** @type {WalkStep[]} */
  const steps = [];
  const legs = Array.isArray(r0?.legs) ? r0.legs : [];
  for (const leg of legs) {
    const sl = Array.isArray(leg?.steps) ? leg.steps : [];
    for (const st of sl) {
      const ni = st.navigationInstruction && typeof st.navigationInstruction === 'object' ? st.navigationInstruction : {};
      const instr = String(ni.instructions || ni.maneuver || '').trim();
      const lv = st.localizedValues && typeof st.localizedValues === 'object' ? st.localizedValues : {};
      const dist = lv.distance && typeof lv.distance === 'object' ? lv.distance : {};
      const distText = String(dist.text || '').trim();
      const end = extractLatLng(st.endLocation);
      steps.push({
        instr: instr || 'Continue',
        distText,
        endLat: end.lat,
        endLng: end.lng,
      });
    }
  }
  let destLat = /** @type {number | null} */ (null);
  let destLng = /** @type {number | null} */ (null);
  if (steps.length) {
    const last = steps[steps.length - 1];
    destLat = last.endLat;
    destLng = last.endLng;
  }
  if ((destLat == null || destLng == null) && poly.length) {
    const L = poly[poly.length - 1];
    destLat = L.lat;
    destLng = L.lng;
  }
  return { poly, steps, destLat, destLng };
}

let _lastCue = '';

function speakNav(text) {
  const t = String(text || '').trim();
  if (!t) return;
  _lastCue = t;
  speechBus.speak(t, { priority: 'nav', rate: 1.05 });
}

export function lastNavCue() {
  return _lastCue;
}

const ROUTE_CACHE_KEY = 'seeingaeye-walk-route';

function saveWalkRouteCache(address, plan) {
  try {
    localStorage.setItem(
      ROUTE_CACHE_KEY,
      JSON.stringify({
        address: address.trim().toLowerCase(),
        savedAt: Date.now(),
        poly: plan.poly,
        steps: plan.steps,
        destLat: plan.destLat,
        destLng: plan.destLng,
      })
    );
  } catch {
    /* storage full or blocked */
  }
}

function loadWalkRouteCache(address) {
  try {
    const raw = localStorage.getItem(ROUTE_CACHE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || data.address !== address.trim().toLowerCase()) return null;
    if (!Array.isArray(data.poly) || !data.poly.length) return null;
    const ageMs = Date.now() - Number(data.savedAt || 0);
    if (ageMs > 12 * 60 * 60 * 1000) return null;
    return data;
  } catch {
    return null;
  }
}

async function fetchWalkRoute(apiBase, originLat, originLng, address) {
  const r = await fetch(`${apiBase}/api/maps/routes/compute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      origin: { latitude: originLat, longitude: originLng },
      destination: { address },
      travel_mode: 'WALK',
      include_llm_markdown: false,
    }),
  });
  if (!r.ok) {
    const err = await r.text();
    throw new Error(err || `Routes HTTP ${r.status}`);
  }
  return r.json();
}

async function ensureOrientationPermission() {
  try {
    if (typeof DeviceOrientationEvent !== 'undefined' && typeof DeviceOrientationEvent.requestPermission === 'function') {
      const perm = await DeviceOrientationEvent.requestPermission();
      if (perm !== 'granted') return false;
    }
  } catch {
    return false;
  }
  return true;
}

function readCompassHeading(e) {
  const oe = /** @type {DeviceOrientationEvent & { webkitCompassHeading?: number }} */ (e);
  if (typeof oe.webkitCompassHeading === 'number' && !Number.isNaN(oe.webkitCompassHeading)) {
    return oe.webkitCompassHeading;
  }
  if (e.absolute && typeof e.alpha === 'number' && !Number.isNaN(e.alpha)) {
    return (360 - e.alpha + 360) % 360;
  }
  return null;
}

/**
 * @param {{ destinationAddress: string, apiBase?: string, onStatus?: (s: string) => void, getFreshGps?: () => Promise<{ lat: number, lon: number } | null> }} opts
 */
export async function startLiveWalkNavigation(opts) {
  stopLiveWalkNavigation();
  const address = (opts.destinationAddress || '').trim();
  const apiBase = opts.apiBase ?? '';
  const onStatus = opts.onStatus ?? (() => {});
  if (!address) {
    onStatus('Enter a destination first.');
    return;
  }
  onStatus('Requesting compass (if needed)…');
  await ensureOrientationPermission();

  onStatus('Getting your position…');
  let pos = opts.getFreshGps
    ? await opts.getFreshGps()
    : await new Promise((resolve) => {
        if (!navigator.geolocation) {
          resolve(null);
          return;
        }
        navigator.geolocation.getCurrentPosition(
          (p) => resolve({ lat: p.coords.latitude, lon: p.coords.longitude }),
          () => resolve(null),
          { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 }
        );
      });
  if (!pos) {
    onStatus('Could not read GPS. Allow location and try again.');
    return;
  }

  onStatus('Fetching walking route…');
  let plan = null;
  let usedCache = false;
  const cached = !navigator.onLine ? loadWalkRouteCache(address) : null;
  if (cached) {
    plan = cached;
    usedCache = true;
  } else {
    try {
      const data = await fetchWalkRoute(apiBase, pos.lat, pos.lon, address);
      plan = parseRouteRaw(data.raw);
    } catch (e) {
      const fallback = loadWalkRouteCache(address);
      if (!fallback) {
        onStatus(String(e?.message || e));
        speakNav('I could not load a route, and there is no saved route for this place.');
        return;
      }
      plan = fallback;
      usedCache = true;
    }
  }
  if (!plan || (!plan.steps?.length && !plan.poly?.length)) {
    onStatus('No walking route returned. Check Maps API / destination.');
    return;
  }
  if (!plan.poly.length) {
    onStatus('Route has no polyline in API response — check GOOGLE_MAPS_ROUTES_FIELD_MASK includes route polyline.');
    return;
  }

  const destLat = plan.destLat;
  const destLng = plan.destLng;
  if (destLat == null || destLng == null) {
    onStatus('Route has no destination coordinates.');
    return;
  }

  _state = {
    poly: plan.poly,
    steps: plan.steps,
    destLat,
    destLng,
    stepIndex: 0,
    lastAnnounce: 0,
    lastPhrase: '',
    offRouteSince: null,
    nearSpoken: false,
    wrongSpokenAt: 0,
    headingSmooth: null,
    arrivedSpoken: false,
    approachFor: -1,
    rerouteBusy: false,
    lastReroute: 0,
    lastGpsErr: 0,
    apiBase,
    destinationAddress: address,
    offlineRoute: usedCache,
  };

  if (!usedCache) saveWalkRouteCache(address, plan);

  onStatus(
    usedCache
      ? 'Using the saved walking route. The network is unavailable, so this may be out of date.'
      : 'Walk assist on — follow voice cues. Hold phone flat for side hints.'
  );

  if (usedCache) {
    speakNav('The network is down. I am using the last saved walking route. It may be out of date.');
  }
  const first = plan.steps?.[0];
  if (first?.instr) {
    const d = first.distText ? ` About ${first.distText}.` : '';
    speakNav(`Starting route. ${first.instr}.${d}`);
  } else {
    speakNav('Starting walking directions.');
  }

  if (typeof window !== 'undefined') {
    _orientHandler = (e) => {
      if (!_state) return;
      const h = readCompassHeading(/** @type {DeviceOrientationEvent} */ (e));
      if (h == null) return;
      const prev = _state.headingSmooth;
      if (prev == null) _state.headingSmooth = h;
      else _state.headingSmooth = prev * 0.82 + h * 0.18;
    };
    window.addEventListener('deviceorientationabsolute', _orientHandler, true);
    window.addEventListener('deviceorientation', _orientHandler, true);
  }

  _watchId = navigator.geolocation?.watchPosition(
    (p) => {
       if (!_state) return;
       const lat = p.coords.latitude;
       const lng = p.coords.longitude;
       const here = { lat, lng };
       const now = Date.now();
       const s = _state;

       const dPoly = minDistanceToPolylineMeters(here, s.poly);
       if (dPoly > OFF_ROUTE_M) {
         if (s.offRouteSince == null) s.offRouteSince = now;
         else if (
           now - s.offRouteSince > WRONG_DIR_HOLD_MS &&
           !s.rerouteBusy &&
           now - s.lastReroute > 45000
         ) {
           s.rerouteBusy = true;
           s.lastReroute = now;
           s.wrongSpokenAt = now;
           speakNav('You left the walking path. I am calculating a new route from where you are.');
           onStatus('Recalculating route.');
           fetchWalkRoute(s.apiBase, lat, lng, s.destinationAddress)
             .then((data) => {
               if (!_state) return;
               const next = parseRouteRaw(data.raw);
               if (!next?.poly?.length) {
                 speakNav('I could not calculate a new route. Stop and ask for directions again.');
                 return;
               }
               _state.poly = next.poly;
               _state.steps = next.steps;
               _state.stepIndex = 0;
               _state.approachFor = -1;
               _state.nearSpoken = false;
               _state.arrivedSpoken = false;
               _state.offRouteSince = null;
               if (next.destLat != null && next.destLng != null) {
                 _state.destLat = next.destLat;
                 _state.destLng = next.destLng;
               }
               saveWalkRouteCache(s.destinationAddress, {
                 poly: _state.poly,
                 steps: _state.steps,
                 destLat: _state.destLat,
                 destLng: _state.destLng,
               });
               const first = next.steps[0];
               speakNav(
                 first?.instr
                   ? `New route. ${first.instr}${first.distText ? `. About ${first.distText}.` : ''}`
                   : 'New route ready. Continue ahead.'
               );
               onStatus('New route from your current position.');
             })
             .catch(() => {
               speakNav('I could not calculate a new route. Stay put, then try again.');
               onStatus('Could not recalculate.');
             })
             .finally(() => {
               if (_state) _state.rerouteBusy = false;
             });
         }
       } else {
         s.offRouteSince = null;
       }

       const step = s.steps[s.stepIndex];
       if (step && step.endLat != null && step.endLng != null) {
         const toEnd = distMeters(here, { lat: step.endLat, lng: step.endLng });
         if (
           toEnd < 80 &&
           toEnd > STEP_REACHED_M &&
           s.approachFor !== s.stepIndex &&
           !s.rerouteBusy
         ) {
           s.approachFor = s.stepIndex;
           const meters = Math.round(toEnd);
           speakNav(`About ${meters} meters. ${step.instr}`);
         }
         if (toEnd < STEP_REACHED_M && s.stepIndex < s.steps.length - 1) {
           s.stepIndex += 1;
           s.approachFor = -1;
           const next = s.steps[s.stepIndex];
           const phrase = next.instr + (next.distText ? `. About ${next.distText}.` : '');
           if (phrase && (phrase !== s.lastPhrase || now - s.lastAnnounce > ANNOUNCE_THROTTLE_MS)) {
             s.lastAnnounce = now;
             s.lastPhrase = phrase;
             speakNav(phrase);
           }
         }
       }

       const dDest = distMeters(here, { lat: s.destLat, lng: s.destLng });
       if (dDest < 12 && !s.arrivedSpoken) {
         s.arrivedSpoken = true;
         s.nearSpoken = true;
         speakNav('You are at the destination area. Turn slowly until the entrance is in front of you.');
         onStatus('Arrived (approximate).');
         return;
       }

       if (dDest < NEAR_DEST_M && !s.nearSpoken) {
         s.nearSpoken = true;
         const bearTo = bearingDegrees(here, { lat: s.destLat, lng: s.destLng });
         const head = s.headingSmooth;
         const side = sideRelativeToFacing(head, bearTo);
         let extra = 'You are very close.';
         if (side === 'left') extra += ' The destination should be on your left.';
         else if (side === 'right') extra += ' The destination should be on your right.';
         else if (side === 'ahead') extra += ' It should be mostly ahead of you.';
         else if (side === 'behind') extra += ' It may be behind you. Turn around.';
         else extra += ' I am not sure which side. Hold the phone flat and turn slowly.';
         speakNav(extra);
         onStatus('Very close to destination.');
       }
     },
    (err) => {
      if (!_state) return;
      const now = Date.now();
      if (now - (_state.lastGpsErr || 0) < 20000) return;
      _state.lastGpsErr = now;
      const denied = err && err.code === 1;
      speakNav(
        denied
          ? 'Location permission is off. Allow location to keep guiding you.'
          : 'I lost your location. Stay where you are until the signal returns.'
      );
      onStatus(denied ? 'Location permission denied.' : 'Location signal lost.');
    },
    { enableHighAccuracy: true, maximumAge: 3000, timeout: 20000 }
  );
}

export function stopLiveWalkNavigation() {
  if (_watchId != null && navigator.geolocation) {
    navigator.geolocation.clearWatch(_watchId);
    _watchId = null;
  }
  if (_orientHandler && typeof window !== 'undefined') {
    window.removeEventListener('deviceorientationabsolute', _orientHandler, true);
    window.removeEventListener('deviceorientation', _orientHandler, true);
    _orientHandler = null;
  }
  _state = null;
  speechBus.stopAllSpeech();
}

export function isWalkNavActive() {
  return _watchId != null;
}
