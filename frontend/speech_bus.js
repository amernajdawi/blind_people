/**
 * One speaker for chat and walking cues.
 * Navigation speech goes first and does not get cut off by a chat reply.
 */

let _token = 0;
/** @type {{ priority: string, onend?: () => void, token: number } | null} */
let _active = null;
/** @type {{ text: string, priority: string, rate?: number, onstart?: () => void, onend?: () => void }[]} */
const _queue = [];

function _finishActive(token) {
  if (!_active || _active.token !== token) return;
  const cb = _active.onend;
  _active = null;
  try {
    cb?.();
  } catch {
    /* ignore */
  }
  _pump();
}

function _pump() {
  if (_active || !_queue.length || typeof window === 'undefined' || !window.speechSynthesis) return;
  const job = _queue.shift();
  if (!job) return;
  const token = ++_token;
  const u = new SpeechSynthesisUtterance(job.text);
  u.rate = job.rate ?? (job.priority === 'nav' ? 1.05 : 1.02);
  u.lang = document.documentElement.lang || 'en';
  _active = { priority: job.priority, onend: job.onend, token };
  u.onstart = () => {
    try {
      job.onstart?.();
    } catch {
      /* ignore */
    }
  };
  u.onend = () => _finishActive(token);
  u.onerror = () => _finishActive(token);
  window.speechSynthesis.speak(u);
}

/**
 * @param {string} text
 * @param {{ priority?: 'nav' | 'chat' | 'status', rate?: number, onstart?: () => void, onend?: () => void }} [opts]
 */
export function speak(text, opts = {}) {
  const cleaned = String(text || '').trim();
  if (!cleaned || typeof window === 'undefined' || !window.speechSynthesis) {
    opts.onend?.();
    return;
  }
  const job = {
    text: cleaned,
    priority: opts.priority || 'chat',
    rate: opts.rate,
    onstart: opts.onstart,
    onend: opts.onend,
  };
  if (job.priority === 'nav') {
    for (let i = _queue.length - 1; i >= 0; i--) {
      if (_queue[i].priority !== 'nav') {
        const dropped = _queue.splice(i, 1)[0];
        try {
          dropped.onend?.();
        } catch {
          /* ignore */
        }
      }
    }
    _queue.unshift(job);
    if (_active && _active.priority !== 'nav') {
      const cb = _active.onend;
      _active = null;
      _token += 1;
      try {
        window.speechSynthesis.cancel();
      } catch {
        /* ignore */
      }
      try {
        cb?.();
      } catch {
        /* ignore */
      }
    }
    _pump();
    return;
  }
  _queue.push(job);
  _pump();
}

export function stopAllSpeech() {
  _token += 1;
  const callbacks = [];
  if (_active?.onend) callbacks.push(_active.onend);
  _active = null;
  while (_queue.length) {
    const job = _queue.shift();
    if (job?.onend) callbacks.push(job.onend);
  }
  try {
    window.speechSynthesis?.cancel();
  } catch {
    /* ignore */
  }
  for (const cb of callbacks) {
    try {
      cb();
    } catch {
      /* ignore */
    }
  }
}
