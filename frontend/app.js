import * as speechBus from './speech_bus.js';
import * as walkNav from './walk_nav.js';

const cam = document.getElementById('cam');
const healthEl = document.getElementById('health');
const outJson = document.getElementById('out-json');
const cameraSelect = document.getElementById('camera-device');
const btnRefreshCams = document.getElementById('btn-refresh-cameras');
const gemmaCard = document.getElementById('gemma-card');
const gemmaStatus = document.getElementById('gemma-status');
const btnGemmaSpeak = document.getElementById('btn-gemma-speak');
const btnGemmaStopSpeech = document.getElementById('btn-gemma-stop-speech');

/** @type {string} */
let lastGemmaReply = '';
let _gemmaDotsId = null;
/** @type {{ role: string, content: string }[]} */
let gemmaChatHistory = [];
let _gemmaBusy = false;

const LiveSpeechApi =
  typeof window !== 'undefined' ? window.SpeechRecognition || window.webkitSpeechRecognition : null;

let _liveActive = false;
/** @type {any} */
let _recognition = null;
let _liveTtsActive = false;
let _lastLiveFinal = '';
let _lastLiveFinalAt = 0;

/** Cached geolocation for Modality A + web (weather). @type {{ lat: number, lon: number, accuracy: number | null, ts: number } | null} */
let _cachedGeo = null;
const GEO_CACHE_MS = 10 * 60 * 1000;

function useDeviceLocationEnabled() {
  const el = document.getElementById('use-device-location');
  return el?.checked !== false;
}

/**
 * @param {{ highAccuracy?: boolean, maximumAge?: number }} [opts]
 * - `maximumAge: 0` asks the OS for a new fix (best effort) instead of reusing a stale reading.
 */
function requestDeviceLocationOnce(opts = {}) {
  const high = opts.highAccuracy === true;
  const maxAge =
    typeof opts.maximumAge === 'number'
      ? opts.maximumAge
      : high
        ? 0
        : 120000;
  return new Promise((resolve) => {
    if (typeof navigator === 'undefined' || !navigator.geolocation) {
      resolve(null);
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        resolve({
          lat: pos.coords.latitude,
          lon: pos.coords.longitude,
          accuracy: Number.isFinite(pos.coords.accuracy) ? pos.coords.accuracy : null,
          ts: Date.now(),
        });
      },
      () => resolve(null),
      {
        enableHighAccuracy: high,
        timeout: high ? 20000 : 15000,
        maximumAge: maxAge,
      }
    );
  });
}

/**
 * Resolves when a position is available, or null if disabled / denied.
 * @param {{ forceFresh?: boolean }} [opts] If `forceFresh`, skips the in-app cache and asks the browser/OS for a current fix (`maximumAge: 0`, higher accuracy). Use for map/directions so answers match **live** location.
 */
async function getCachedDeviceLocation(opts = {}) {
  if (!useDeviceLocationEnabled()) return null;
  const forceFresh = opts.forceFresh === true;
  if (!forceFresh && _cachedGeo && Date.now() - _cachedGeo.ts < GEO_CACHE_MS) {
    return _cachedGeo;
  }
  const g = await requestDeviceLocationOnce({
    highAccuracy: forceFresh,
    maximumAge: forceFresh ? 0 : 120000,
  });
  _cachedGeo = g;
  return g;
}

const gemmaChatLog = document.getElementById('gemma-chat-log');
const gemmaChatInput = document.getElementById('gemma-chat-input');

function apiBase() {
  return '';
}

/** Fetch with timeout (broader support than AbortSignal.timeout). */
async function fetchWithTimeout(url, timeoutMs = 8000) {
  const ctrl = new AbortController();
  const id = window.setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { signal: ctrl.signal });
  } finally {
    window.clearTimeout(id);
  }
}

async function checkHealth() {
  const ollamaEl = document.getElementById('ollama-status');
  try {
    const r = await fetchWithTimeout(`${apiBase()}/api/health`, 8000);
    if (!r.ok) throw new Error(String(r.status));
    const j = await r.json();
    healthEl.textContent = 'API ready';
    healthEl.classList.add('ok');
    healthEl.classList.remove('bad');
    if (ollamaEl && j.ollama) {
      ollamaEl.textContent = j.ollama.configured
        ? `Gemma: ${j.ollama.model}`
        : 'Gemma: set OLLAMA_API_KEY in .env';
    }
    return j;
  } catch (e) {
    healthEl.textContent = 'API offline — run docker compose';
    healthEl.classList.add('bad');
    healthEl.classList.remove('ok');
    if (ollamaEl) ollamaEl.textContent = '';
    throw e;
  }
}

/** @param {number} maxSide longest edge in px (smaller = faster upload) */
function snapshotBlob(maxSide = 768) {
  return new Promise((resolve, reject) => {
    const vw = cam.videoWidth;
    const vh = cam.videoHeight;
    if (!vw || !vh) {
      reject(new Error('no video frame'));
      return;
    }
    let tw = vw;
    let th = vh;
    if (Math.max(vw, vh) > maxSide) {
      const s = maxSide / Math.max(vw, vh);
      tw = Math.round(vw * s);
      th = Math.round(vh * s);
    }
    const tmp = document.createElement('canvas');
    tmp.width = tw;
    tmp.height = th;
    const tctx = tmp.getContext('2d');
    tctx.drawImage(cam, 0, 0, tw, th);
    tmp.toBlob((b) => (b ? resolve(b) : reject(new Error('snapshot failed'))), 'image/jpeg', 0.78);
  });
}

/** Prefer enumerateDevices(); Chrome often omits IDs briefly — live track settings still carry deviceId. */
function mergeEnumerateWithLiveTracks(videos) {
  const stream = cam?.srcObject;
  if (!stream?.getVideoTracks) return videos;
  const out = videos.slice();
  for (const track of stream.getVideoTracks()) {
    const settings = track.getSettings?.() || {};
    const id = String(settings.deviceId || '').trim();
    if (!id) continue;
    if (out.some((d) => String(d.deviceId || '').trim() === id)) continue;
    out.push({
      kind: 'videoinput',
      deviceId: id,
      label: String(track.label || '').trim() || 'Camera',
    });
  }
  return out;
}

/**
 * @param {{ tryUnlock?: boolean }} [opts] If true, may call getUserMedia once to reveal device labels/IDs (use after user gesture).
 */
async function refreshCameraList(opts = {}) {
  if (!cameraSelect || !navigator.mediaDevices?.enumerateDevices) return;

  const enumerateVideoInputs = async () => {
    const list = await navigator.mediaDevices.enumerateDevices();
    return list.filter((d) => d.kind === 'videoinput');
  };

  const scoreEnumerate = (arr) =>
    arr.filter((d) => String(d.deviceId || '').trim()).length * 100 +
    arr.filter((d) => String(d.label || '').trim()).length;

  let videos = await enumerateVideoInputs();

  const hasRealIds = videos.some((d) => String(d.deviceId || '').trim());
  const labelsMissing = videos.length > 0 && videos.every((d) => !String(d.label || '').trim());
  const streamActive = Boolean(cam?.srcObject?.getVideoTracks?.().length);

  /* Only probe after a user gesture (tryUnlock). Cold load must not call getUserMedia — it often fails and leaves empty deviceIds. */
  const needsPermissionProbe =
    opts.tryUnlock &&
    !streamActive &&
    (videos.length === 0 || !hasRealIds || labelsMissing);

  if (needsPermissionProbe) {
    try {
      const tmp = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      tmp.getTracks().forEach((t) => t.stop());
      await new Promise((r) => setTimeout(r, 200));
      videos = await enumerateVideoInputs();
      await new Promise((r) => setTimeout(r, 150));
      const again = await enumerateVideoInputs();
      if (scoreEnumerate(again) > scoreEnumerate(videos)) videos = again;
    } catch {
      /* permission denied or no camera */
    }
  }

  if (streamActive) {
    for (const ms of [150, 400, 800]) {
      await new Promise((r) => setTimeout(r, ms));
      const next = await enumerateVideoInputs();
      if (scoreEnumerate(next) >= scoreEnumerate(videos)) videos = next;
      videos = mergeEnumerateWithLiveTracks(videos);
    }
  }

  videos = mergeEnumerateWithLiveTracks(videos);

  const keep = cameraSelect.value;
  cameraSelect.innerHTML = '';
  const opt0 = document.createElement('option');
  opt0.value = '';
  opt0.textContent = 'Default (system choice)';
  cameraSelect.appendChild(opt0);

  const seen = new Set();
  let idx = 0;
  for (const d of videos) {
    const id = String(d.deviceId || '').trim();
    if (!id) continue;
    if (seen.has(id)) continue;
    seen.add(id);
    idx += 1;
    const o = document.createElement('option');
    o.value = id;
    const name = String(d.label || '').trim();
    o.textContent = name || `Camera ${idx}`;
    cameraSelect.appendChild(o);
  }

  if ([...cameraSelect.options].some((o) => o.value === keep && !o.disabled)) {
    cameraSelect.value = keep;
  }
}

function stopWebcam() {
  if (cam?.srcObject) {
    cam.srcObject.getTracks().forEach((t) => t.stop());
    cam.srcObject = null;
  }
  if (gemmaStatus) gemmaStatus.textContent = 'Camera off. You can still talk for weather, places, and directions.';
}

function videoConstraints(deviceId) {
  /* Avoid fixed resolution + facingMode on desktop — Chrome often opens a “dead” stream on USB webcams. */
  if (deviceId) {
    return { video: { deviceId: { ideal: deviceId } }, audio: false };
  }
  return { video: true, audio: false };
}

async function startWebcam() {
  if (!cam) throw new Error('Video element #cam not found — reload from this app’s index page.');
  if (typeof window !== 'undefined' && window.isSecureContext === false) {
    throw new Error(
      'Camera needs a secure page. Use http://localhost:8085 (or your port) on this machine, or https:// — plain http://YOUR-LAN-IP often blocks camera.'
    );
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error(
      'Camera API unavailable. Use https:// or http://localhost (not an insecure LAN IP), and a current Chrome / Edge / Safari.'
    );
  }
  const deviceId = cameraSelect?.value || '';

  function stopCamPreview() {
    if (cam.srcObject) {
      cam.srcObject.getTracks().forEach((t) => t.stop());
      cam.srcObject = null;
    }
  }

  stopCamPreview();

  /** @type {MediaStreamConstraints[]} */
  const attempts = [videoConstraints(deviceId)];
  if (deviceId) {
    attempts.push({
      video: { deviceId: { ideal: deviceId }, width: { max: 960 }, height: { max: 540 } },
      audio: false,
    });
  }
  attempts.push({ video: true, audio: false });

  const seen = new Set();
  const uniq = attempts.filter((c) => {
    const key = JSON.stringify(c);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  let lastError = /** @type {Error | null} */ (null);

  for (const constraints of uniq) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia(constraints);
      for (const t of stream.getVideoTracks()) {
        t.enabled = true;
      }
      cam.srcObject = stream;
      cam.muted = true;
      cam.playsInline = true;
      cam.setAttribute('playsinline', '');
      await cam.play();
      try {
        await waitForVideoReady(6500);
      } catch {
        stopCamPreview();
        lastError = new Error(
          'Camera opened but no video frames. Try another camera in the list, unplug/replug the USB webcam, or close other apps using the camera.'
        );
        continue;
      }
      await refreshCameraList({ tryUnlock: false }).catch((err) => console.warn(err));
      return;
    } catch (e) {
      stopCamPreview();
      const name = e?.name || '';
      if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
        throw new Error(
          'Camera permission denied. Allow camera for this site in the browser address bar, then tap Start webcam again.'
        );
      }
      if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
        lastError = new Error('No camera found. Connect a webcam or pick another device in the list.');
        continue;
      }
      if (name === 'OverconstrainedError' || name === 'NotReadableError' || name === 'TrackStartError') {
        lastError = new Error(e?.message || String(e));
        continue;
      }
      lastError = new Error(e?.message || String(e));
    }
  }

  throw (
    lastError ||
    new Error('Could not start camera. Use Refresh list, pick a camera, then Start webcam again.')
  );
}

/** Video often reports width/height only after `loadeddata` — without this, Save / snapshot can no-op. */
function waitForVideoReady(timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    if (cam.videoWidth > 0 && cam.videoHeight > 0) {
      resolve();
      return;
    }
    const t0 = Date.now();
    const tryOk = () => {
      if (cam.videoWidth > 0 && cam.videoHeight > 0) {
        cleanup();
        resolve();
      } else if (Date.now() - t0 > timeoutMs) {
        cleanup();
        reject(new Error('Camera not ready — wait until you see the picture, then try again.'));
      }
    };
    const iv = setInterval(tryOk, 50);
    const onVid = () => tryOk();
    cam.addEventListener('loadeddata', onVid);
    cam.addEventListener('loadedmetadata', onVid);
    function cleanup() {
      clearInterval(iv);
      cam.removeEventListener('loadeddata', onVid);
      cam.removeEventListener('loadedmetadata', onVid);
    }
  });
}

function cleanGemmaText(s) {
  if (!s) return '';
  return s
    .replace(/<\|channel>thought\s*\n[\s\S]*?<channel\|>/gi, '')
    .replace(/<\|[^>]+\|>/g, '')
    .trim();
}

function setGemmaMode(mode) {
  if (!gemmaCard) return;
  gemmaCard.classList.remove('is-active', 'is-looking', 'is-streaming', 'is-voice');
  if (mode === 'looking') gemmaCard.classList.add('is-active', 'is-looking');
  else if (mode === 'streaming') gemmaCard.classList.add('is-active', 'is-streaming');
  else if (mode === 'voice') gemmaCard.classList.add('is-active', 'is-voice');
}

function stopGemmaStatusDots() {
  if (_gemmaDotsId) clearInterval(_gemmaDotsId);
  _gemmaDotsId = null;
}

/** @param {string} base */
function startGemmaStatusDots(base) {
  stopGemmaStatusDots();
  let n = 0;
  _gemmaDotsId = setInterval(() => {
    n = (n + 1) % 4;
    if (gemmaStatus) gemmaStatus.textContent = base + (n ? '.'.repeat(n) : '');
  }, 420);
}

function trimGemmaHistory() {
  const max = 24;
  if (gemmaChatHistory.length > max) gemmaChatHistory = gemmaChatHistory.slice(-max);
}

function syncLiveListeningUi() {
  if (gemmaCard) {
    gemmaCard.classList.toggle('is-listening', _liveActive && !_gemmaBusy && !_liveTtsActive);
  }
  const btn = document.getElementById('btn-live-toggle');
  if (btn) {
    btn.textContent = _liveActive ? 'Stop live' : 'Start live';
    btn.classList.toggle('is-on', _liveActive);
  }
}

function scheduleLiveListenRestart() {
  if (!_liveActive || _gemmaBusy || _liveTtsActive) return;
  try {
    _recognition?.start();
  } catch {
    /* already running */
  }
}

function stopLiveGemma() {
  _liveActive = false;
  window.speechSynthesis?.cancel();
  _liveTtsActive = false;
  if (_recognition) {
    try {
      _recognition.onend = null;
      _recognition.stop();
    } catch {
      /* ignore */
    }
    _recognition = null;
  }
  _gemmaBusy = false;
  const bs = document.getElementById('btn-gemma-send');
  const bl = document.getElementById('btn-llm');
  if (bs) bs.disabled = false;
  if (bl) bl.disabled = false;
  stopGemmaStatusDots();
  setGemmaMode('idle');
  syncLiveListeningUi();
  if (gemmaStatus) {
    gemmaStatus.textContent = cam.srcObject
      ? 'Live off — type below or tap Start live when you want voice again.'
      : 'Ready — turn on your camera and chat with me.';
  }
}

function startLiveGemma() {
  if (_appLocked) {
    const msg = 'Unlock the app first.';
    if (gemmaStatus) gemmaStatus.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
    return;
  }
  if (!LiveSpeechApi) {
    const msg = 'Live voice needs a browser with speech recognition. Chrome or Edge works best.';
    if (gemmaStatus) gemmaStatus.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
    return;
  }
  if (_liveActive) return;
  _liveActive = true;
  _recognition = new LiveSpeechApi();
  _recognition.continuous = true;
  _recognition.interimResults = true;
  _recognition.lang = navigator.language || 'en-US';

  _recognition.onresult = (ev) => {
    let interim = '';
    let finalChunk = '';
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      const t = r[0].transcript;
      if (r.isFinal) finalChunk += t;
      else interim += t;
    }
    if (interim.trim() && gemmaStatus) gemmaStatus.textContent = 'Listening: ' + interim.trim();
    const said = finalChunk.trim();
    if (!said) return;
    const now = Date.now();
    if (said === _lastLiveFinal && now - _lastLiveFinalAt < 1800) return;
    if (said.length < 2) return;
    if (tryVoiceCommand(said)) {
      _lastLiveFinal = said;
      _lastLiveFinalAt = now;
      return;
    }
    if (_gemmaBusy || _liveTtsActive) return;
    _lastLiveFinal = said;
    _lastLiveFinalAt = now;
    void onLiveFinalUtterance(said);
  };

  _recognition.onerror = (ev) => {
    if (ev.error === 'no-speech' || ev.error === 'aborted') return;
    if (ev.error === 'not-allowed') {
      stopLiveGemma();
      const msg = 'Microphone blocked. Allow the microphone, then start live again.';
      if (gemmaStatus) gemmaStatus.textContent = msg;
      speechBus.speak(msg, { priority: 'status' });
      return;
    }
    const msg = 'I could not hear you. Check the microphone and try again.';
    if (gemmaStatus) gemmaStatus.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
  };

  _recognition.onend = () => {
    if (_liveActive && !_gemmaBusy) {
      setTimeout(scheduleLiveListenRestart, 280);
    }
  };

  try {
    _recognition.start();
  } catch (e) {
    console.warn(e);
    _liveActive = false;
    _recognition = null;
    if (gemmaStatus) gemmaStatus.textContent = 'Could not start the microphone — try again.';
    syncLiveListeningUi();
    return;
  }
  syncLiveListeningUi();
  if (gemmaStatus) {
    gemmaStatus.textContent = cam?.srcObject
      ? 'Listening. Ask me anything.'
      : 'Listening. Camera is off, so I can answer with voice, maps, and weather. Turn the camera on when you want me to look.';
  }
}

function tryVoiceCommand(said) {
  const text = said.trim().replace(/[.!?]+$/g, '');
  if (_appLocked) return false;
  if (/^(stop talking|be quiet|quiet|shut up|cancel voice)$/i.test(text) || /^stop$/i.test(text)) {
    speechBus.stopAllSpeech();
    _liveTtsActive = false;
    if (gemmaStatus) gemmaStatus.textContent = 'Stopped.';
    return true;
  }
  if (/^stop (walking|navigation|navigating|directions|guiding)$/i.test(text)) {
    walkNav.stopLiveWalkNavigation();
    syncWalkNavUi();
    if (walkNavStatusEl) walkNavStatusEl.textContent = 'Walk assist stopped.';
    speechBus.speak('Walking guidance stopped.', { priority: 'status' });
    return true;
  }
  if (/^(repeat|repeat that|say that again|what did you say|repeat the step)$/i.test(text)) {
    const cue = walkNav.isWalkNavActive() ? walkNav.lastNavCue() : '';
    const line = (cue || lastGemmaReply || '').trim();
    speechBus.speak(line || 'I have nothing to repeat yet.', { priority: 'status' });
    return true;
  }
  if (/^(check ahead|check the path( ahead)?|what(?:'s| is) in front of me|can i cross)$/i.test(text)) {
    void checkPathAhead();
    return true;
  }
  const go = text.match(/^(?:please\s+)?(?:take me to|navigate to|guide me to|walk me to)\s+(.+)$/i);
  if (go && go[1].trim().length > 1) {
    void startWalkTo(go[1].trim());
    return true;
  }
  return false;
}

async function onLiveFinalUtterance(spoken) {
  try {
    _recognition?.stop();
  } catch {
    /* ignore */
  }

  _gemmaBusy = true;
  syncLiveListeningUi();
  const btnSend = document.getElementById('btn-gemma-send');
  const btnLlm = document.getElementById('btn-llm');
  if (btnSend) btnSend.disabled = true;
  if (btnLlm) btnLlm.disabled = true;

  setGemmaMode('looking');
  if (gemmaStatus)
    gemmaStatus.textContent = blindAssistRouterEnabled()
      ? 'Listening… routing your question.'
      : 'One moment…';
  startGemmaStatusDots('One moment');

  gemmaChatHistory.push({ role: 'user', content: spoken });
  trimGemmaHistory();
  const uRow = appendGemmaChatRow('user', '🎤 ' + spoken);
  const rowAsst = appendGemmaChatRow('assistant', '…');
  rowAsst.classList.add('streaming');
  const outLlm = document.getElementById('out-llm');

  const finishLiveCycle = () => {
    _gemmaBusy = false;
    stopGemmaStatusDots();
    if (btnSend) btnSend.disabled = false;
    if (btnLlm) btnLlm.disabled = false;
    syncLiveListeningUi();
    if (!_liveActive) return;
    try {
      _recognition?.start();
    } catch {
      setTimeout(scheduleLiveListenRestart, 320);
    }
  };

  try {
    const msgs = gemmaChatHistory.map((m) => ({ role: m.role, content: m.content }));
    /** @type {{ reply?: string | null }} */
    let j;
    const onPartial = (partial) => {
      const t = cleanGemmaText(partial) || '…';
      if (outLlm) outLlm.textContent = t;
      rowAsst.textContent = t;
    };
    const onVisionStreaming = () => {
      stopGemmaStatusDots();
      setGemmaMode('streaming');
      if (gemmaStatus) gemmaStatus.textContent = "Here's what I notice…";
    };
    const onTextStreaming = () => {
      stopGemmaStatusDots();
      setGemmaMode('streaming');
      if (gemmaStatus) gemmaStatus.textContent = "Here's my take";
    };

    if (blindAssistRouterEnabled()) {
      j = await runBlindAssist(spoken, msgs, onPartial);
    } else {
      const route = await resolveAssistantRoute(spoken);
      if (gemmaStatus) gemmaStatus.textContent = route.reasonShort;

      if (route.mod === 'A') {
        j = await postTextChat(msgs, onPartial, onTextStreaming, {
          webGrounding: true,
        });
      } else if (route.mod === 'B') {
        if (!cam.srcObject || !cam.videoWidth) {
          throw new Error('Turn on the camera for this sight question.');
        }
        await waitForVideoReady(8000).catch(() => {});
        const blob = await snapshotBlob(768);
        j = await postVisionLlm(blob, spoken, onPartial, onVisionStreaming);
      } else {
        if (!cam.srcObject || !cam.videoWidth) {
          throw new Error('Turn on the camera — this question needs a short clip of frames.');
        }
        await waitForVideoReady(8000).catch(() => {});
        const blobs = await captureModalityFrames(cam.srcObject, route.frames, route.clipMs);
        const hint = `\n Frames are chronological (~${route.clipMs / 1000}s clip, ${route.frames} samples). Combine for motion or layout.`;
        j = await postVisionLlmMulti(blobs, spoken + hint, onPartial, onVisionStreaming);
      }
    }

    if (!_liveActive) {
      const finalText = j.reply || cleanGemmaText(rowAsst.textContent) || '';
      rowAsst.textContent = finalText || '(empty reply)';
      rowAsst.classList.remove('streaming');
      if (outLlm) outLlm.textContent = finalText;
      gemmaChatHistory.push({ role: 'assistant', content: finalText });
      trimGemmaHistory();
      lastGemmaReply = finalText;
      finishLiveCycle();
      return;
    }

    const finalText = j.reply || cleanGemmaText(rowAsst.textContent) || '';
    rowAsst.textContent = finalText || '(empty reply)';
    rowAsst.classList.remove('streaming');
    if (outLlm) outLlm.textContent = finalText;
    gemmaChatHistory.push({ role: 'assistant', content: finalText });
    trimGemmaHistory();
    lastGemmaReply = finalText;
    setGemmaMode('idle');

    speakTextAloud(finalText, { kind: 'live', onLiveComplete: finishLiveCycle });
  } catch (e) {
    gemmaChatHistory.pop();
    uRow?.remove();
    rowAsst?.remove();
    stopGemmaStatusDots();
    setGemmaMode('idle');
    if (gemmaStatus) gemmaStatus.textContent = String(e.message || e);
    speakTextAloud(String(e.message || e), { kind: 'live', onLiveComplete: finishLiveCycle });
    return;
  }
}

/**
 * Read text aloud (manual button or live reply).
 * @param {string} text
 * @param {{ kind?: 'manual' | 'live', onLiveComplete?: () => void }} [opts]
 */
function speakTextAloud(text, opts = {}) {
  const kind = opts.kind || 'manual';
  const cleaned = cleanGemmaText(String(text || '').replace(/^—\s*$/, '').trim());
  if (!cleaned || cleaned === '—') {
    if (kind === 'manual' && gemmaStatus) {
      gemmaStatus.textContent = 'Nothing to read yet — ask me something first.';
    }
    if (kind === 'live') opts.onLiveComplete?.();
    return;
  }
  if (!window.speechSynthesis) {
    if (gemmaStatus) gemmaStatus.textContent = 'Your browser does not support read-aloud.';
    if (kind === 'live') opts.onLiveComplete?.();
    return;
  }
  speechBus.speak(cleaned, {
    priority: 'chat',
    onstart: () => {
      if (kind === 'live') _liveTtsActive = true;
      setGemmaMode('voice');
      if (gemmaStatus) {
        gemmaStatus.textContent =
          kind === 'live' ? "I'm answering out loud" : "I'm reading that out loud for you";
      }
      if (btnGemmaStopSpeech) btnGemmaStopSpeech.hidden = false;
      syncLiveListeningUi();
    },
    onend: () => {
      if (kind === 'live') _liveTtsActive = false;
      setGemmaMode('idle');
      if (btnGemmaStopSpeech) btnGemmaStopSpeech.hidden = true;
      if (gemmaStatus) {
        if (kind === 'live' && _liveActive) {
          gemmaStatus.textContent = 'Listening — your turn.';
        } else {
          gemmaStatus.textContent = 'Done — want me to look again?';
        }
      }
      if (kind === 'live') opts.onLiveComplete?.();
      syncLiveListeningUi();
    },
  });
}

/**
 * @param {'user'|'assistant'} role
 * @param {string} text
 */
function appendGemmaChatRow(role, text) {
  if (!gemmaChatLog) return null;
  const row = document.createElement('div');
  row.className = `gemma-chat-row ${role}`;
  row.textContent = text;
  gemmaChatLog.appendChild(row);
  gemmaChatLog.scrollTop = gemmaChatLog.scrollHeight;
  return row;
}

async function parseNdjsonStream(r, onPartial, onStreamStart) {
  const reader = r.body?.getReader();
  if (!reader) throw new Error('No response body');
  const dec = new TextDecoder();
  let buffer = '';
  let assembled = '';
  let streamStarted = false;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += dec.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line.trim()) continue;
      let o;
      try {
        o = JSON.parse(line);
      } catch {
        continue;
      }
      if (o.error) throw new Error(o.error);
      if (o.content) {
        if (!streamStarted && onStreamStart) {
          streamStarted = true;
          onStreamStart();
        }
        assembled += o.content;
        if (onPartial) onPartial(assembled);
      }
      if (o.done && o.reply != null) {
        return { reply: o.reply, model: o.model };
      }
    }
  }
  return { reply: cleanGemmaText(assembled), model: null };
}

async function fetchErrorMessage(r) {
  let msg = `${r.status}`;
  try {
    const j = await r.json();
    msg = j.detail || j.message || msg;
    if (Array.isArray(msg)) msg = msg.map((x) => x.msg || x).join('; ');
  } catch {
    msg = (await r.text()) || msg;
  }
  return msg;
}

// --- Blind-assist modality router (A = text-only, B = one frame, C = short clip ≤9 frames) ---
// Modality A merges default Gemma persona + server-side blind addendum (see blind_assist flag); no long hard-coded prompt here.
/**
 * Uses server modality router (heuristic + tiny LLM when ambiguous); falls back locally if offline.
 * @returns {Promise<{ mod: string, frames: number, clipMs: number, reasonShort: string }>}
 */
async function resolveAssistantRoute(userText) {
  const t = String(userText || '').trim();
  if (!t) {
    return { mod: 'A', frames: 1, clipMs: 3200, reasonShort: 'Modality A — empty question.' };
  }
  try {
    const r = await fetch(`${apiBase()}/api/assistant/route`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: t }),
    });
    const j = await r.json();
    if (!r.ok)
      throw new Error(j.detail ? String(j.detail) : `${r.status}`);
    return {
      mod: String(j.modality || 'A').toUpperCase().slice(0, 1),
      frames: Number(j.frames) || 7,
      clipMs: Number(j.clip_ms) || 4200,
      reasonShort: String(j.reason_short || ''),
    };
  } catch (e) {
    console.warn('assistant/route fallback:', e?.message ?? e);
    return classifyBlindModality(t);
  }
}

function blindAssistRouterEnabled() {
  const el = document.getElementById('blind-assist-auto');
  return el?.checked !== false;
}

const HAZARD_LOOK_RE =
  /\b(check ahead|in front of me|ahead of me|can i cross|safe to (walk|cross)|curb|kerb|traffic|obstacle|obstacles|steps ahead|stairs ahead)\b/i;

function hazardLookRequested(text) {
  return HAZARD_LOOK_RE.test(text || '');
}

function hazardLookInstructions() {
  return [
    'Hazard check for a person who cannot see.',
    'Speak in this order: first say stop or continue, then name only what the picture supports (curb, step, hole, pole, vehicle, bicycle, or person) and which side.',
    'If the picture is dark, blurry, or you cannot tell, say stop and wait.',
    'This is a camera description, not a traffic light or a promise that the street is safe.',
  ].join(' ');
}

/** @param {string} userQ */
function blindVisionPrompt(userQ) {
  const q = (userQ || '').trim();
  const hazard = hazardLookRequested(q) ? `\n\n${hazardLookInstructions()}` : '';
  return `${q}\n\n[Assume the listener cannot see. Speak clearly for text-to-speech. Avoid "as you can see". The photo is the camera view, not a mirror: left in the image is the person's left when the camera faces forward. Use left, right, ahead, and behind.]${hazard}`;
}

/**
 * Weather/temperature for "outside / today" must use text+GPS path, not vision — unless they mean the photo.
 * @param {string} raw
 * @param {'A'|'B'|'C'} mod
 */
function finalizeBlindModality(raw, mod) {
  if (mod === 'A') return mod;
  const lower = (raw || '').toLowerCase();
  const photoOk =
    /\b(in this (photo|picture|frame|image)|from my camera|webcam|on screen|see (the|this) thermometer|read (the|this) thermometer|temperature on (the|this|my))\b/i.test(
      lower
    );
  if (photoOk) return mod;
  const wx =
    /\b(weather|forecast|temperature|celsius|fahrenheit|raining|rains|snow|humidity|how cold|how hot|degrees\b)\b/i.test(
      lower
    );
  const loc =
    /\b(where am i|where are we|where'?s my location|where is my location|what is my location|\bmy location\b|what'?s my location|what city am i|what town am i in)\b/i.test(
      lower
    );
  const webLookup =
    /\b(?:(?:can|could)\s+you\s+(?:please\s+)?(?:check|google|search)|(?:please\s+)?(?:check|test)\s+(?:my\s+|the\s+)?internet\b|google\b|(?:search|ask)\s+google|google\s+(?:if|whether|for|about)|(?:look|search)\s+(?:it|this|that|that\s+up)\s+(?:up\s+)?(?:on\s+)?(?:google|online)|(?:web\s+search|search\s+(?:the\s+)?web|search\s+online)|\bsearch\s+for\b|\bduckduckgo\b|\bbing\b)\b/i.test(
      lower
    );
  if (wx || loc || webLookup) return 'A';
  return mod;
}

/**
 * Lightweight keyword router (no LLM classify call). Prefer A→B→C on ties.
 * @returns {{ mod: 'A'|'B'|'C', frames: number, clipMs: number, reasonShort: string }}
 */
function classifyBlindModality(raw) {
  const q = (raw || '').trim();
  const lower = q.toLowerCase();
  if (!q)
    return { mod: 'A', frames: 1, clipMs: 3200, reasonShort: 'Modality A — empty question.' };

  let scoreA = 0;
  let scoreB = 0;
  let scoreC = 0;

  if (
    /\b(weather|forecast|temperature|celsius|fahrenheit|raining|rains|snow|humidity|how cold|how hot|degrees\b)\b/i.test(
      lower
    )
  )
    scoreA += 3;
  if (
    /\b(where am i|where are we|where'?s my location|where is my location|what is my location|\bmy location\b|what'?s my location|what city am i|what town am i in)\b/i.test(
      lower
    )
  )
    scoreA += 3;
  if (/\b(news|headlines)\b/i.test(lower)) scoreA += 3;
  if (
    /\b(?:(?:can|could)\s+you\s+(?:please\s+)?(?:check|google|search)|(?:please\s+)?(?:check|test)\s+(?:my\s+|the\s+)?internet\b|google\b|(?:search|ask)\s+google|google\s+(?:if|whether|for|about)|(?:web\s+search|search\s+(?:the\s+)?web|search\s+online)|\bsearch\s+for\b)\b/i.test(
      lower
    )
  )
    scoreA += 3;
  if (/\bcapital of\b/i.test(lower)) scoreA += 2;
  if (/\bcalculate\b|convert.*\b(to|into)\b|^\s*[\d.]+\s*[\+\-*\/]/i.test(lower)) scoreA += 3;
  if (/\bmiles to\b|kilometers\b|distance\b.*\bbetween\b|\bhow far\b/i.test(lower)) scoreA += 2;
  if (/^what time|^what'?s the time\b|timezone\b|current date\b/i.test(lower)) scoreA += 2;
  if (/\b(who won|final score|playoff bracket)\b/i.test(lower)) scoreA += 2;
  if (/\b(who (is|was|are)|meaning of life|capitalism|explain (the theory|concept))\b/i.test(lower))
    scoreA += 1;
  if (
    /\bwhat (is|'s|'re)\b/i.test(lower) &&
    !/\b(this|that|these|here|holding|in front of|camera|frame|desk|wallet|cup|shirt|dress|tie)\b/i.test(
      lower
    )
  )
    scoreA += 2;
  if (/\b(history of|tell me about|describe (the planet|mars|jupiter))\b/i.test(lower)) scoreA += 1;

  if (
    /\b(read|spell|dosage|ingredients\b|medicine bottle|pill bottle|label on|fine print)\b/i.test(lower)
  )
    scoreB += 4;
  if (/\b(barcode|qr code|receipt|menu|price tag|sign\b|poster|captions)\b/i.test(lower))
    scoreB += 3;
  if (/\bwhat colour|what color|which colour|which color\b/i.test(lower)) scoreB += 2;
  if (/\b(what'?s this|what is this|identify this object|what am i holding)\b/i.test(lower))
    scoreB += 3;
  if (
    /\b(describe what's in front|describe what'?s ahead|what'?s on (my desk|the table))\b/i.test(lower)
  )
    scoreB += 2;

  if (/\bwhere('s|\s+is|\s+are|\s+would)\b/i.test(lower) && /\b(door|exit|stairs|bathroom|kitchen|elevator|keys|wallet|cup|charger|bag|glass|remote)\b/i.test(lower))
    scoreC += 4;
  else if (
    /\b(where('s|\s+is|\s+are|\s+would)|locate|find the|lost my|finding the)\b/i.test(lower)
  )
    scoreC += 2;
  if (/\b(layout|whole room|scan (the room|around)|orient (myself)?|sense of space)\b/i.test(lower))
    scoreC += 3;
  if (/\b(navigate\b|toward the|nearest exit\b|walking path)\b/i.test(lower)) scoreC += 2;
  if (/\b(guide me|cooking|cook\b|knife\b|pour(ing|\b)|liquid\b|moving toward|approaching|someone (coming|walking))\b/i.test(lower))
    scoreC += 3;
  if (/\b(camera|webcam|in the frame|this frame|live video|describe the picture|what do you see)\b/i.test(lower))
    scoreB += 3;
  if (hazardLookRequested(lower)) scoreB += 6;

  const scores = { A: scoreA, B: scoreB, C: scoreC };
  const rank = { A: 0, B: 1, C: 2 };
  let chosen = 'A';
  for (const m of ['B', 'C']) {
    const sm = scores[m];
    const sc = scores[chosen];
    if (sm > sc) chosen = m;
    else if (sm === sc && rank[m] < rank[chosen]) chosen = m;
  }

  chosen = finalizeBlindModality(q, chosen);

  const plan = planClipForC(lower, chosen);
  const reasonShort = `[${chosen}] ${plan.reasonLine}`;
  return { mod: chosen, frames: plan.frames, clipMs: plan.clipMs, reasonShort };
}

/**
 * @param {string} lower
 * @param {'A'|'B'|'C'} mod
 */
function planClipForC(lower, mod) {
  if (mod !== 'C')
    return {
      frames: 1,
      clipMs: 3200,
      reasonLine:
        mod === 'A'
          ? 'Text / thinking — no camera capture this turn.'
          : 'Single snapshot — static scene or text.',
    };

  let n = 7;
  if (/\bquick\b|brief scan|fast\b/i.test(lower)) n -= 2;
  if (/\b(find my|find the|locate|where('s|\s+is)\b)/i.test(lower)) n = Math.max(n, 7);
  if (/\b(layout|whole room|scan\b|orient\b)/i.test(lower)) n = Math.max(n, 8);
  if (/\b(approaching|movement|walking toward|motion)\b/i.test(lower)) n = Math.min(n, 5);
  if (/\b(pour|cooking|cook\b|spill)\b/i.test(lower)) n = Math.max(5, Math.min(n, 7));
  n = Math.max(3, Math.min(9, n));
  const clipMs = n <= 4 ? 3200 : n >= 8 ? 5500 : 4200;
  return {
    frames: n,
    clipMs,
    reasonLine: `Video sample — ${n} frames from ~${(clipMs / 1000).toFixed(1)}s clip.`,
  };
}

function pickRecorderMime() {
  if (typeof MediaRecorder === 'undefined') return '';
  const types = ['video/webm;codecs=vp8', 'video/webm', 'video/mp4'];
  return types.find((t) => MediaRecorder.isTypeSupported(t)) ?? '';
}

/**
 * @param {MediaStream} stream
 * @param {number} durationMs
 * @returns {Promise<Blob>}
 */
function recordVideoClip(stream, durationMs) {
  const mimeType = pickRecorderMime();
  return new Promise((resolve, reject) => {
    const chunks = [];
    let mr;
    try {
      mr = mimeType
        ? new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 2_500_000 })
        : new MediaRecorder(stream);
    } catch (e) {
      reject(e);
      return;
    }
    mr.onerror = () => reject(new Error('MediaRecorder failed'));
    mr.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunks.push(e.data);
    };
    mr.onstop = () => resolve(new Blob(chunks, { type: mimeType || 'video/webm' }));
    try {
      mr.start(140);
      setTimeout(() => {
        try {
          mr.stop();
        } catch (e2) {
          reject(e2);
        }
      }, durationMs);
    } catch (e) {
      reject(e);
    }
  });
}

/**
 * @param {Blob} blob
 * @param {number} frameCount 3–9
 * @param {number} maxSide
 */
async function extractFramesEvenlyFromRecording(blob, frameCount, maxSide = 768) {
  const url = URL.createObjectURL(blob);
  /** @type {HTMLVideoElement} */
  const video = document.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.preload = 'auto';

  await new Promise((resolve, reject) => {
    const ok = () => resolve(null);
    const bad = () => reject(new Error('Could not decode the camera clip'));
    video.addEventListener('loadedmetadata', ok, { once: true });
    video.addEventListener('error', bad, { once: true });
    video.src = url;
  });

  const duration = Math.max(0.08, Number(video.duration || 3));
  /** @type {Blob[]} */
  const blobs = [];

  try {
    for (let i = 0; i < frameCount; i++) {
      const t = frameCount === 1 ? 0 : Math.min(duration * (i / (frameCount - 1)), duration - 0.04);
      video.currentTime = t;
      await new Promise((r) => {
        video.onseeked = () => r(null);
      });
      const vw = video.videoWidth;
      const vh = video.videoHeight;
      if (!vw || !vh) throw new Error('no decoded video size');
      let tw = vw;
      let th = vh;
      if (Math.max(vw, vh) > maxSide) {
        const s = maxSide / Math.max(vw, vh);
        tw = Math.round(vw * s);
        th = Math.round(vh * s);
      }
      const tmp = document.createElement('canvas');
      tmp.width = tw;
      tmp.height = th;
      const tctx = tmp.getContext('2d');
      tctx.drawImage(video, 0, 0, tw, th);
      const b = await new Promise((res, rej) =>
        tmp.toBlob((xb) => (xb ? res(xb) : rej(new Error('jpeg frame encode failed'))), 'image/jpeg', 0.78)
      );
      blobs.push(b);
    }
    return blobs;
  } finally {
    URL.revokeObjectURL(url);
    video.src = '';
    video.remove();
  }
}

/**
 * @param {number} count
 * @param {number} spacingMs
 * @returns {Promise<Blob[]>}
 */
async function timedSnapshotBurst(count, spacingMs = 420) {
  /** @type {Blob[]} */
  const out = [];
  for (let i = 0; i < count; i++) {
    out.push(await snapshotBlob(768));
    if (i < count - 1)
      await new Promise((r) => {
        setTimeout(r, spacingMs);
      });
  }
  return out;
}

/**
 * @param {MediaStream|null|undefined} stream
 * @param {number} frameCount
 * @param {number} clipMs
 */
async function captureModalityFrames(stream, frameCount, clipMs) {
  try {
    if (stream?.getTracks?.()?.length && pickRecorderMime()) {
      const rec = await recordVideoClip(stream, clipMs);
      return await extractFramesEvenlyFromRecording(rec, frameCount, 768);
    }
    throw new Error('no recorder');
  } catch (e) {
    console.warn('Clip capture fallback:', e?.message ?? e);
    return timedSnapshotBurst(frameCount);
  }
}

/**
 * @param {Blob[]} blobs
 * @param {string} prompt
 */
async function postVisionLlmMulti(blobs, prompt, onPartial, onStreamStart, visionOpts = {}) {
  if (!blobs?.length) throw new Error('no frames');
  if (blobs.length > 9) throw new Error('at most 9 frames');

  const fd = new FormData();
  blobs.forEach((b, idx) => fd.append('files', b, `frame_${idx + 1}.jpg`));
  fd.append('prompt', prompt);
  fd.append('stream', 'true');
  if (visionOpts.systemPrompt?.trim()) {
    fd.append('system_prompt', visionOpts.systemPrompt.trim());
  }
  const r = await fetch(`${apiBase()}/api/llm/vision/multi`, { method: 'POST', body: fd });
  if (!r.ok) throw new Error(await fetchErrorMessage(r));
  return parseNdjsonStream(r, onPartial, onStreamStart);
}

/**
 * Runs blind routing: returns streamed LLM reply object (same as postTextChat / vision).
 *
 * @param {string} userPrompt Plain user question text
 * @param {{ role: string, content: string }[]} messagesForChat Conversation for modality A only
 * @param {{ onVisionStart?: () => void, onTextStart?: () => void, cachedRoute?: { mod: string, frames: number, clipMs: number, reasonShort: string } }} [hooks]
 */
async function runBlindAssist(userPrompt, messagesForChat, onPartial, hooks = {}) {
  function defaultTextStart() {
    stopGemmaStatusDots();
    setGemmaMode('streaming');
    if (gemmaStatus) gemmaStatus.textContent = "Here's my take";
  }
  function defaultVisionStart() {
    stopGemmaStatusDots();
    setGemmaMode('streaming');
    if (gemmaStatus) gemmaStatus.textContent = "Here's what I notice…";
  }
  const visionStartCb = hooks.onVisionStart ?? defaultVisionStart;
  const textStartCb = hooks.onTextStart ?? defaultTextStart;

  const route = hooks.cachedRoute ?? (await resolveAssistantRoute(userPrompt));
  if (gemmaStatus) gemmaStatus.textContent = route.reasonShort;

  if (route.mod === 'A') {
    return await postTextChat(
      messagesForChat.map((m) => ({ role: m.role, content: m.content })),
      onPartial,
      textStartCb,
      { blindAssist: true, webGrounding: true }
    );
  }

  if (!cam.srcObject || !cam.videoWidth) {
    throw new Error('Turn on the webcam — this question looks like it needs sight.');
  }
  await waitForVideoReady(8000).catch(() => {});

  if (route.mod === 'B') {
    const blob = await snapshotBlob(768);
    const q = blindVisionPrompt(userPrompt);
    return await postVisionLlm(blob, q, onPartial, visionStartCb);
  }

  const blobs = await captureModalityFrames(cam.srcObject, route.frames, route.clipMs);
  const hint = `\n Frames are chronological from a ~${route.clipMs / 1000}s clip (${route.frames} samples). Combine them for spatial or motion cues.`;
  const q = blindVisionPrompt(userPrompt + hint);
  return await postVisionLlmMulti(blobs, q, onPartial, visionStartCb);
}

/**
 * @param {(partial: string) => void} [onPartial]
 * @param {() => void} [onStreamStart]
 * @param {{ systemPrompt?: string }} [visionOpts]
 */
async function postVisionLlm(blob, prompt, onPartial, onStreamStart, visionOpts = {}) {
  const fd = new FormData();
  fd.append('file', blob, 'frame.jpg');
  fd.append('prompt', prompt);
  fd.append('stream', 'true');
  if (visionOpts.systemPrompt?.trim()) {
    fd.append('system_prompt', visionOpts.systemPrompt.trim());
  }
  const r = await fetch(`${apiBase()}/api/llm/vision`, { method: 'POST', body: fd });
  if (!r.ok) throw new Error(await fetchErrorMessage(r));
  return parseNdjsonStream(r, onPartial, onStreamStart);
}

/**
 * @param {{ role: string, content: string }[]} messages
 * @param {(partial: string) => void} [onPartial]
 * @param {() => void} [onStreamStart]
 * @param {{ systemPrompt?: string, webGrounding?: boolean, blindAssist?: boolean, useLangchain?: boolean }} [opts]
 */
async function postTextChat(messages, onPartial, onStreamStart, opts = {}) {
  const payload = { messages, stream: true };
  if (opts.useLangchain) payload.use_langchain = true;
  if (opts.blindAssist) {
    payload.blind_assist = true;
    if (opts.systemPrompt?.trim()) payload.system_prompt = opts.systemPrompt.trim();
  } else if (opts.systemPrompt?.trim()) {
    payload.system_prompt = opts.systemPrompt.trim();
  }
  if (opts.webGrounding) {
    payload.web_grounding = true;
    const loc = await getCachedDeviceLocation({ forceFresh: true });
    if (loc) {
      payload.latitude = loc.lat;
      payload.longitude = loc.lon;
      if (loc.accuracy != null) payload.location_accuracy_m = loc.accuracy;
    }
  }
  const r = await fetch(`${apiBase()}/api/llm/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(await fetchErrorMessage(r));
  return parseNdjsonStream(r, onPartial, onStreamStart);
}

btnRefreshCams?.addEventListener('click', () => {
  refreshCameraList({ tryUnlock: true }).catch((e) => console.warn(e));
});

cameraSelect?.addEventListener('change', () => {
  if (!cam?.srcObject) return;
  startWebcam().catch((e) => {
    const msg = `Camera switch failed: ${e.message || e}`;
    if (outJson) outJson.textContent = msg;
    if (gemmaStatus) gemmaStatus.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
  });
});

document.getElementById('btn-stop-cam')?.addEventListener('click', () => {
  stopWebcam();
});

document.getElementById('btn-check-ahead')?.addEventListener('click', () => {
  void checkPathAhead();
});

async function checkPathAhead() {
  if (_appLocked) {
    speechBus.speak('Unlock the app first.', { priority: 'status' });
    return;
  }
  if (_gemmaBusy) return;
  if (!cam?.srcObject || !cam.videoWidth) {
    const msg = 'Turn the camera on, then ask me to check the path ahead.';
    if (gemmaStatus) gemmaStatus.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
    return;
  }
  _gemmaBusy = true;
  const btn = document.getElementById('btn-check-ahead');
  if (btn) btn.disabled = true;
  if (gemmaStatus) gemmaStatus.textContent = 'Looking at the path ahead.';
  try {
    const blob = await snapshotBlob(768);
    const prompt = `Check the path ahead.\n\n${hazardLookInstructions()}`;
    const j = await postVisionLlm(blob, prompt);
    const reply = (j?.reply || '').trim();
    if (reply) {
      lastGemmaReply = reply;
      const outLlm = document.getElementById('out-llm');
      if (outLlm) outLlm.textContent = reply;
      speakTextAloud(reply, { kind: 'manual' });
    } else {
      speechBus.speak('I could not describe the path. Stop and wait.', { priority: 'status' });
    }
  } catch (e) {
    const msg = String(e?.message || e);
    if (gemmaStatus) gemmaStatus.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
  } finally {
    _gemmaBusy = false;
    if (btn) btn.disabled = false;
  }
}

document.getElementById('btn-start')?.addEventListener('click', async () => {
  try {
    await startWebcam();
    await refreshCameraList({ tryUnlock: true });
    if (gemmaStatus) gemmaStatus.textContent = 'Camera on. Live voice can start.';
    const auto = document.getElementById('live-gemma-autostart');
    if (auto && auto.checked) startLiveGemma();
  } catch (e) {
    const msg = `Webcam: ${e.message || e}`;
    if (outJson) outJson.textContent = msg;
    if (gemmaStatus) gemmaStatus.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
  }
});

document.getElementById('btn-live-toggle')?.addEventListener('click', () => {
  if (_liveActive) stopLiveGemma();
  else startLiveGemma();
});

function seedGemmaChat() {
  if (!gemmaChatLog || gemmaChatLog.querySelector('.gemma-chat-row')) return;
  appendGemmaChatRow(
    'assistant',
    "Hi — I'm Gemma. Talk naturally. I use voice for weather and directions, and the camera when you ask what is in front of you. You can start live voice without the camera."
  );
}

async function sendGemmaText() {
  if (_appLocked) {
    speechBus.speak('Unlock the app first.', { priority: 'status' });
    return;
  }
  if (_gemmaBusy || !gemmaChatInput) return;
  const text = gemmaChatInput.value.trim();
  if (!text) return;
  gemmaChatInput.value = '';
  _gemmaBusy = true;
  const btnSend = document.getElementById('btn-gemma-send');
  const btnLlmBlock = document.getElementById('btn-llm');
  if (btnSend) btnSend.disabled = true;
  if (btnLlmBlock) btnLlmBlock.disabled = true;
  setGemmaMode('looking');
  if (gemmaStatus) gemmaStatus.textContent = 'Thinking with you';
  startGemmaStatusDots('Thinking with you');

  const uRow = appendGemmaChatRow('user', text);
  gemmaChatHistory.push({ role: 'user', content: text });
  trimGemmaHistory();
  const rowAsst = appendGemmaChatRow('assistant', '…');
  rowAsst.classList.add('streaming');
  const outLlm = document.getElementById('out-llm');

  try {
    const msgs = gemmaChatHistory.map((m) => ({ role: m.role, content: m.content }));
    /** @type {{ reply?: string | null }} */
    let j;
    const onPartialCb = (partial) => {
      const t = cleanGemmaText(partial) || '…';
      rowAsst.textContent = t;
      if (outLlm) outLlm.textContent = t;
    };
    const streamStartText = () => {
      stopGemmaStatusDots();
      setGemmaMode('streaming');
      if (gemmaStatus) gemmaStatus.textContent = "Here's my take";
    };
    if (blindAssistRouterEnabled()) {
      j = await runBlindAssist(text, msgs, onPartialCb);
    } else {
      j = await postTextChat(msgs, onPartialCb, streamStartText, { webGrounding: true });
    }
    const finalText = j.reply || cleanGemmaText(rowAsst.textContent) || '';
    rowAsst.textContent = finalText || '(empty reply)';
    rowAsst.classList.remove('streaming');
    if (outLlm) outLlm.textContent = finalText;
    gemmaChatHistory.push({ role: 'assistant', content: finalText });
    trimGemmaHistory();
    lastGemmaReply = finalText;
    setGemmaMode('idle');
    if (gemmaStatus) gemmaStatus.textContent = 'What else is on your mind?';
    if (blindAssistRouterEnabled() && finalText) {
      speakTextAloud(finalText, { kind: 'manual' });
    }
  } catch (e) {
    gemmaChatHistory.pop();
    uRow?.remove();
    rowAsst.textContent = String(e.message || e);
    rowAsst.classList.remove('streaming');
    setGemmaMode('idle');
    if (gemmaStatus) gemmaStatus.textContent = 'That request failed — try again?';
  } finally {
    stopGemmaStatusDots();
    _gemmaBusy = false;
    if (btnSend) btnSend.disabled = false;
    if (btnLlmBlock) btnLlmBlock.disabled = false;
    syncLiveListeningUi();
  }
}

document.getElementById('btn-gemma-send')?.addEventListener('click', () => sendGemmaText());
gemmaChatInput?.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault();
    sendGemmaText();
  }
});
document.getElementById('btn-gemma-clear')?.addEventListener('click', () => {
  gemmaChatHistory = [];
  if (gemmaChatLog) gemmaChatLog.innerHTML = '';
  seedGemmaChat();
  if (gemmaStatus) gemmaStatus.textContent = 'Fresh start — say anything you like.';
});

const walkNavDest = document.getElementById('walk-nav-destination');
const btnWalkNavStart = document.getElementById('btn-walk-nav-start');
const btnWalkNavStop = document.getElementById('btn-walk-nav-stop');
const walkNavStatusEl = document.getElementById('walk-nav-status');

function syncWalkNavUi() {
  const active = walkNav.isWalkNavActive();
  if (btnWalkNavStart) btnWalkNavStart.disabled = active;
  if (btnWalkNavStop) btnWalkNavStop.disabled = !active;
}

async function startWalkTo(dest) {
  const place = (dest || '').trim();
  if (walkNavDest && place) walkNavDest.value = place;
  if (!place) {
    const msg = 'Say where you want to go.';
    if (walkNavStatusEl) walkNavStatusEl.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
    return;
  }
  if (!useDeviceLocationEnabled()) {
    const msg = 'Turn on location, then ask me to take you there.';
    if (walkNavStatusEl) walkNavStatusEl.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
    return;
  }
  if (walkNavStatusEl) walkNavStatusEl.textContent = '…';
  if (btnWalkNavStart) btnWalkNavStart.disabled = true;
  speechBus.speak(`Starting the walk to ${place}.`, { priority: 'status' });
  try {
    await walkNav.startLiveWalkNavigation({
      destinationAddress: place,
      apiBase: apiBase(),
      onStatus: (t) => {
        if (walkNavStatusEl) walkNavStatusEl.textContent = t;
      },
      getFreshGps: async () => {
        const g = await requestDeviceLocationOnce({ highAccuracy: true, maximumAge: 0 });
        return g ? { lat: g.lat, lon: g.lon } : null;
      },
    });
  } finally {
    syncWalkNavUi();
  }
}

btnWalkNavStart?.addEventListener('click', () => {
  void startWalkTo(walkNavDest?.value || '');
});

btnWalkNavStop?.addEventListener('click', () => {
  walkNav.stopLiveWalkNavigation();
  syncWalkNavUi();
  if (walkNavStatusEl) walkNavStatusEl.textContent = 'Walk assist stopped.';
});

document.getElementById('btn-llm')?.addEventListener('click', async () => {
  const outLlm = document.getElementById('out-llm');
  const promptEl = document.getElementById('llm-prompt');
  const btnLlm = document.getElementById('btn-llm');
  if (_gemmaBusy) return;

  const rawPrompt = (promptEl && promptEl.value) || '';
  const camQ = rawPrompt.trim() || 'What do you see in my camera right now?';
  const routingText = camQ.trim();
  const blind = blindAssistRouterEnabled();
  const routeGuess = blind ? await resolveAssistantRoute(routingText) : null;

  if (!blind) {
    if (!cam.srcObject || !cam.videoWidth) {
      if (gemmaStatus) gemmaStatus.textContent = 'Turn on the camera first — I need to see you.';
      if (outLlm) outLlm.textContent = 'Start the webcam for camera questions.';
      return;
    }
  } else if (routeGuess && routeGuess.mod !== 'A' && (!cam.srcObject || !cam.videoWidth)) {
    if (gemmaStatus)
      gemmaStatus.textContent = 'Turn on the camera — that question needs a snapshot or clip.';
    if (outLlm) outLlm.textContent = 'Start the webcam for sight-based questions.';
    return;
  }

  _gemmaBusy = true;
  let uRow = null;
  let rowAsst = null;
  const btnSendBlock = document.getElementById('btn-gemma-send');

  setGemmaMode('looking');
  if (gemmaStatus)
    gemmaStatus.textContent = blind && routeGuess ? routeGuess.reasonShort : 'Let me look at your frame';
  startGemmaStatusDots(blind ? 'One moment' : 'Let me look at your frame');
  if (outLlm) outLlm.textContent = '…';
  btnLlm.disabled = true;
  if (btnSendBlock) btnSendBlock.disabled = true;

  gemmaChatHistory.push({ role: 'user', content: camQ });
  trimGemmaHistory();
  uRow = appendGemmaChatRow('user', '📷 ' + camQ);
  rowAsst = appendGemmaChatRow('assistant', '…');
  rowAsst.classList.add('streaming');

  try {
    const msgs = gemmaChatHistory.map((m) => ({ role: m.role, content: m.content }));
    const streamHooks = blind
      ? {
          onVisionStart: () => {
            stopGemmaStatusDots();
            setGemmaMode('streaming');
            if (gemmaStatus)
              gemmaStatus.textContent = "I'm right here — here's what I'm noticing";
          },
          onTextStart: () => {
            stopGemmaStatusDots();
            setGemmaMode('streaming');
            if (gemmaStatus) gemmaStatus.textContent = "Here's my take";
          },
        }
      : {};

    /** @type {{ reply?: string | null }} */
    let j;
    const onPartialCam = (partial) => {
      const t = cleanGemmaText(partial) || '…';
      if (outLlm) outLlm.textContent = t;
      rowAsst.textContent = t;
      if (gemmaStatus && (!blind || (routeGuess && routeGuess.mod !== 'A')))
        gemmaStatus.textContent = "I'm with you — describing what I see";
    };

    if (blind) {
      j = await runBlindAssist(routingText, msgs, onPartialCam, {
        ...streamHooks,
        cachedRoute: routeGuess,
      });
    } else {
      const blob = await snapshotBlob(768);
      j = await postVisionLlm(
        blob,
        rawPrompt,
        onPartialCam,
        () => {
          stopGemmaStatusDots();
          setGemmaMode('streaming');
          if (gemmaStatus)
            gemmaStatus.textContent = "I'm right here — here's what I'm noticing";
        }
      );
    }
    const finalText = j.reply || cleanGemmaText(outLlm ? outLlm.textContent : '') || '';
    if (outLlm) outLlm.textContent = finalText || '(empty reply)';
    if (rowAsst) {
      rowAsst.textContent = finalText || '(empty reply)';
      rowAsst.classList.remove('streaming');
    }
    gemmaChatHistory.push({ role: 'assistant', content: finalText });
    trimGemmaHistory();
    lastGemmaReply = finalText;
    setGemmaMode('idle');
    if (gemmaStatus) {
      gemmaStatus.textContent = "That's what I see — keep chatting or ask again with the camera.";
    }
  } catch (e) {
    gemmaChatHistory.pop();
    uRow?.remove();
    rowAsst?.remove();
    stopGemmaStatusDots();
    setGemmaMode('idle');
    if (gemmaStatus) gemmaStatus.textContent = "Something went wrong — let's try again in a moment.";
    if (outLlm) outLlm.textContent = String(e.message || e);
  } finally {
    btnLlm.disabled = false;
    if (btnSendBlock) btnSendBlock.disabled = false;
    stopGemmaStatusDots();
    _gemmaBusy = false;
    syncLiveListeningUi();
  }
});

function speakLastGemmaReply() {
  const raw = lastGemmaReply || (document.getElementById('out-llm')?.textContent ?? '');
  speakTextAloud(raw, { kind: 'manual' });
}

if (btnGemmaSpeak) {
  btnGemmaSpeak.addEventListener('click', () => speakLastGemmaReply());
}
if (btnGemmaStopSpeech) {
  btnGemmaStopSpeech.addEventListener('click', () => {
    _liveTtsActive = false;
    speechBus.stopAllSpeech();
    btnGemmaStopSpeech.hidden = true;
    setGemmaMode('idle');
    if (_liveActive) {
      _gemmaBusy = false;
      const bs = document.getElementById('btn-gemma-send');
      const bl = document.getElementById('btn-llm');
      if (bs) bs.disabled = false;
      if (bl) bl.disabled = false;
    }
    if (gemmaStatus) {
      gemmaStatus.textContent = _liveActive ? 'Listening — your turn.' : 'Voice stopped.';
    }
    syncLiveListeningUi();
    if (_liveActive) {
      try {
        _recognition?.start();
      } catch {
        setTimeout(scheduleLiveListenRestart, 300);
      }
    }
  });
}

if (navigator.mediaDevices?.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', () => {
    refreshCameraList({ tryUnlock: true }).catch((e) => console.warn(e));
  });
}
let _appLocked = false;

async function refreshAppLock() {
  const lock = document.getElementById('app-lock');
  try {
    const r = await fetch('/api/auth/status');
    if (!r.ok) return;
    const j = await r.json();
    _appLocked = Boolean(j.required && !j.unlocked);
    if (lock) lock.hidden = !_appLocked;
    if (_appLocked) {
      const msg = 'This app is locked. Enter the password, then press Unlock.';
      if (gemmaStatus) gemmaStatus.textContent = msg;
      speechBus.speak(msg, { priority: 'status' });
    }
  } catch {
    /* offline is reported by checkHealth */
  }
}

document.getElementById('btn-app-unlock')?.addEventListener('click', async () => {
  const input = document.getElementById('app-password');
  const status = document.getElementById('app-lock-status');
  const password = input?.value || '';
  if (!password) {
    const msg = 'Type the password first.';
    if (status) status.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
    return;
  }
  const r = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  if (!r.ok) {
    const msg = 'That password was not right.';
    if (status) status.textContent = msg;
    speechBus.speak(msg, { priority: 'status' });
    return;
  }
  _appLocked = false;
  const lock = document.getElementById('app-lock');
  if (lock) lock.hidden = true;
  if (input) input.value = '';
  if (gemmaStatus) gemmaStatus.textContent = 'Unlocked.';
  speechBus.speak('Unlocked. You can talk now.', { priority: 'status' });
});

document.getElementById('app-password')?.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') {
    ev.preventDefault();
    document.getElementById('btn-app-unlock')?.click();
  }
});

refreshCameraList().catch((e) => console.warn(e));

seedGemmaChat();
refreshAppLock().catch(() => {});
checkHealth().catch(() => {});
