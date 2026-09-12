import {EventEmitter} from 'node:events';

export const MAX_BYTES = 8 * 1024 * 1024;
export const MAX_REQUESTS = 10_000;
export const MAX_ID_LENGTH = 256;
export const LONG_POLL_MS = 10_000;
export const MAX_WAITERS = 32;

export class BridgeError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, keys) => object(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const identifier = value => typeof value === 'string' && value.length > 0 && value.length <= MAX_ID_LENGTH;
const invalid = message => { throw new BridgeError(400, message); };

export function validateAdvance(body) {
  if (!exact(body, ['ack', 'request']) || !(body.ack === null || (Number.isSafeInteger(body.ack) && body.ack >= 0)) ||
      !exact(body.request, ['request_id', 'content']) || !identifier(body.request.request_id) || typeof body.request.content !== 'string') {
    invalid('Invalid advance shape');
  }
}

export function validateDecision(value) {
  if (!object(value) || !identifier(value.request_id)) invalid('Invalid decision shape');
  if (value.kind === 'final') {
    if (!exact(value, ['request_id', 'kind', 'text']) || typeof value.text !== 'string') invalid('Invalid final shape');
  } else if (value.kind === 'tool_calls') {
    if (!exact(value, ['request_id', 'kind', 'tool_calls']) || !Array.isArray(value.tool_calls) || value.tool_calls.length < 1 || value.tool_calls.length > 16 ||
        !value.tool_calls.every(call => exact(call, ['name', 'arguments']) && identifier(call.name) && object(call.arguments))) {
      invalid('Invalid tool_calls shape');
    }
  } else invalid('Invalid decision kind');
  if (Buffer.byteLength(JSON.stringify(value)) > MAX_BYTES) throw new BridgeError(413, 'Decision too large');
}

export const RESPOND_SCHEMA = {
  type: 'object',
  properties: {
    request_id: {type: 'string', minLength: 1, maxLength: MAX_ID_LENGTH},
    kind: {type: 'string', enum: ['tool_calls']},
    tool_calls: {type: 'array', minItems: 1, maxItems: 16, items: {
      type: 'object', properties: {
        name: {type: 'string', minLength: 1, maxLength: MAX_ID_LENGTH}, arguments: {type: 'object'},
      }, required: ['name', 'arguments'], additionalProperties: false,
    }},
  },
  // Advertise only proposals. validateDecision still accepts legacy structured
  // finals from an older peer, but new sessions should finish in ordinary text.
  required: ['request_id', 'kind', 'tool_calls'], additionalProperties: false,
};

// The only retained payloads are the current request and the unacknowledged decision.
// IDs are retained for replay detection, with a finite per-process request budget.
export class Bridge extends EventEmitter {
  sequence = 0;
  current = null;
  held = null;
  latest = null;
  failed = null;
  seen = new Set();

  constructor() { super(); this.setMaxListeners(MAX_WAITERS + 1); }
  status() { return {sequence: this.sequence, current: this.current?.request_id ?? null, held: this.held?.sequence ?? null, failed: this.failed}; }
  assertHealthy() { if (this.failed) throw new BridgeError(503, this.failed); }

  advance(body) {
    this.assertHealthy();
    validateAdvance(body);
    if (this.current) throw new BridgeError(409, 'Request already in flight');
    if ((this.latest && body.ack !== this.latest.sequence) || (!this.latest && (this.sequence !== 0 || body.ack !== null))) {
      throw new BridgeError(409, 'Stale or missing acknowledgement');
    }
    if (this.seen.has(body.request.request_id)) throw new BridgeError(409, 'Duplicate request ID');
    if (this.seen.size >= MAX_REQUESTS) {
      this.fail('session_request_limit');
      this.assertHealthy();
    }
    const previous = this.held;
    this.held = null;
    this.latest = null;
    this.current = body.request;
    this.seen.add(body.request.request_id);
    if (previous) {
      previous.cleanup();
      previous.resolve({request: body.request});
    }
    return {initial: previous === null, request: body.request};
  }

  completeText(body) {
    this.assertHealthy();
    if (!exact(body, ['request_id', 'text'])) invalid('Invalid text completion shape');
    const decision = {request_id: body.request_id, kind: 'final', text: body.text};
    validateDecision(decision);
    if (this.held || !this.current || decision.request_id !== this.current.request_id) throw new BridgeError(409, 'Invalid or overlapping response');
    this.current = null;
    this.latest = {sequence: ++this.sequence, ...decision};
    this.emit('change');
    return this.latest;
  }

  respond(decision, signal) {
    this.assertHealthy();
    validateDecision(decision);
    if (this.held || !this.current || decision.request_id !== this.current.request_id) throw new BridgeError(409, 'Invalid or overlapping response');
    if (signal?.aborted) {
      this.fail('native_cancelled');
      this.assertHealthy();
    }
    const response = {sequence: ++this.sequence, ...decision};
    this.current = null;
    const onAbort = () => this.fail('native_cancelled');
    const pending = new Promise((resolve, reject) => {
      this.held = {sequence: this.sequence, resolve, reject, cleanup: () => signal?.removeEventListener('abort', onAbort)};
    });
    signal?.addEventListener('abort', onAbort, {once: true});
    this.latest = response;
    this.emit('change');
    return pending;
  }

  fail(reason) {
    if (this.failed) return;
    this.failed = reason;
    const previous = this.held;
    this.held = null;
    this.current = null;
    this.latest = null;
    if (previous) { previous.cleanup(); previous.reject(new BridgeError(503, reason)); }
    this.emit('change');
  }
}
