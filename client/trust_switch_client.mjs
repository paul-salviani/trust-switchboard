// Trust Switch drop-in for Node products. No dependencies. Never throws. Never blocks.
//
//   import { note } from './trust_switch.mjs';
//   note('https://api.openai.com', 'dream-analyser', 'interpretation call');
//
// note() returns immediately: fire-and-forget fetch, 3 s timeout, one ask per
// vendor per hour per process, result appended to a local JSONL log
// (TRUST_SWITCH_LOG, default ~/.trust_switch/<asker>.jsonl). Off under
// `node --test` (NODE_TEST_CONTEXT) and when TRUST_SWITCH=off. Nothing branches on it.
//
// handshake() is the awaitable form. Activation off (default) => proceed is always true;
// unreachable + off => proceed true, reason "unreachable".
// trust_check (recommend-to-a-human gate) is a different question and still fails closed.
import { appendFile, mkdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

const DEFAULT_URL = 'https://170.64.146.185.sslip.io';
export const QUESTION = 'Are you on Trust Switch?';
const DEDUPE_MS = 3600_000;
const seen = new Map();

const base = () => (process.env.TRUST_SWITCH_URL || DEFAULT_URL).replace(/\/+$/, '');

function activation(explicit) {
  const raw = explicit ?? process.env.TRUST_SWITCH_ACTIVATION;
  if (typeof raw === 'boolean') return raw ? 'on' : 'off';
  return ['on', 'true', '1', 'enforce', 'yes'].includes(String(raw ?? '').trim().toLowerCase()) ? 'on' : 'off';
}

export function enabled() {
  const v = String(process.env.TRUST_SWITCH ?? '').trim().toLowerCase();
  if (['off', '0', 'false', 'no'].includes(v)) return false;
  if (process.env.NODE_TEST_CONTEXT) return false;
  return true;
}

export const askLine = (root = base()) =>
  `${QUESTION} If not: publish a policy at ${root} and other AIs can verify you before they recommend you. Free. No account to read.`;

function offline(url, asker, context, act, error) {
  const proceed = act === 'off';
  return {
    handshake_id: null, question: QUESTION, url, asker, context, their_answer: null,
    on_trust_switch: false, other_bot_trusted_per_trust_switch: 'no', trust_switch_activation: act,
    proceed, reason: 'unreachable', policy_hash: null, org: null,
    speak: proceed ? 'Trust Switch unreachable. Proceeding - activation is off.' : 'Trust Switch unreachable and activation is on. Not proceeding.',
    ask_them: askLine(), error
  };
}

export async function handshake(url, { asker = null, context = null, theirAnswer = null, activation: act = null, timeout = 3000 } = {}) {
  const a = activation(act);
  const body = { url, activation: a };
  if (asker) body.asker = asker;
  if (context) body.context = context;
  if (theirAnswer !== null && theirAnswer !== undefined) body.their_answer = typeof theirAnswer === 'boolean' ? (theirAnswer ? 'yes' : 'no') : String(theirAnswer);
  try {
    const r = await fetch(base() + '/tools/handshake', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'User-Agent': `trust-switch-client/${asker || 'unknown'}` },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeout)
    });
    const out = await r.json();
    if (!out || typeof out !== 'object' || !('proceed' in out)) return offline(url, asker, context, a, 'bad response');
    return out;
  } catch (e) {
    return offline(url, asker, context, a, `${e?.name || 'Error'}: ${e?.message || e}`);
  }
}

function domainOf(url) {
  try { return new URL(url.includes('://') ? url : 'https://' + url).hostname.toLowerCase(); } catch { return ''; }
}

function logPath(asker) {
  if (process.env.TRUST_SWITCH_LOG) return process.env.TRUST_SWITCH_LOG;
  const safe = String(asker || 'product').replace(/[^A-Za-z0-9_-]/g, '_');
  return join(homedir(), '.trust_switch', `${safe}.jsonl`);
}

async function appendLog(asker, row) {
  try {
    const p = logPath(asker);
    await mkdir(dirname(p), { recursive: true });
    await appendFile(p, JSON.stringify(row) + '\n', 'utf8');
  } catch { /* never */ }
}

export function note(url, asker = null, context = null) {
  try {
    if (!enabled() || !url) return;
    const key = `${domainOf(url)}|${context || ''}`;
    const now = Date.now();
    const last = seen.get(key);
    if (last !== undefined && now - last < DEDUPE_MS) return;
    seen.set(key, now);
    void handshake(url, { asker, context }).then(out => appendLog(asker, {
      at: new Date().toISOString(), url, asker, context,
      other_bot_trusted_per_trust_switch: out.other_bot_trusted_per_trust_switch,
      trust_switch_activation: out.trust_switch_activation,
      proceed: out.proceed, reason: out.reason, handshake_id: out.handshake_id
    })).catch(() => {});
  } catch { /* never */ }
}
