import http from 'node:http';
import {timingSafeEqual} from 'node:crypto';
import {BridgeError, LONG_POLL_MS, MAX_BYTES, MAX_WAITERS} from './protocol.mjs';

const BODY_TIMEOUT_MS = 15_000;
const json = (res, status, data) => {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'});
  res.end(JSON.stringify(data));
};

function readBody(req) {
  return new Promise((resolve, reject) => {
    let bytes = 0;
    const chunks = [];
    const cleanup = () => {
      clearTimeout(timer);
      req.off('data', onData); req.off('end', onEnd); req.off('aborted', onAbort); req.off('error', onAbort);
    };
    const fail = error => { cleanup(); reject(error); };
    const onAbort = () => fail(new BridgeError(400, 'Incomplete request body'));
    const onData = chunk => {
      bytes += chunk.length;
      if (bytes > MAX_BYTES) { fail(new BridgeError(413, 'Request body too large')); req.resume(); return; }
      chunks.push(chunk);
    };
    const onEnd = () => {
      cleanup();
      try { resolve(JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(Buffer.concat(chunks)))); }
      catch { reject(new BridgeError(400, 'Invalid JSON body')); }
    };
    const timer = setTimeout(() => fail(new BridgeError(408, 'Request body timeout')), BODY_TIMEOUT_MS);
    req.on('data', onData); req.once('end', onEnd); req.once('aborted', onAbort); req.once('error', onAbort);
  });
}

export function createHttpServer({bridge, token, advance, log = () => {}}) {
  const expected = Buffer.from(`Bearer ${token}`, 'utf8');
  let waiters = 0;
  let readers = 0;
  const server = http.createServer({maxHeaderSize: 16 * 1024}, async (req, res) => {
    try {
      const supplied = Buffer.from(req.headers.authorization ?? '', 'utf8');
      if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
        res.setHeader('Connection', 'close');
        json(res, 401, {error: 'unauthorized'});
        return;
      }
      const url = new URL(req.url, 'http://127.0.0.1');
      const method = {'/advance': 'POST', '/text-complete': 'POST', '/status': 'GET', '/response': 'GET'}[url.pathname];
      if (!method) throw new BridgeError(404, 'Not found');
      if (req.method !== method) { res.setHeader('Allow', method); throw new BridgeError(405, 'Method not allowed'); }
      if (method === 'GET' && (req.headers['transfer-encoding'] || Number(req.headers['content-length'] ?? 0) !== 0)) throw new BridgeError(400, 'GET body not allowed');
      if (url.pathname === '/status') {
        if (url.search) throw new BridgeError(400, 'Unexpected query');
        json(res, 200, bridge.status());
      } else if (url.pathname === '/response') {
        const afterText = url.searchParams.get('after');
        const waitText = url.searchParams.get('wait_ms');
        const keys = [...url.searchParams.keys()];
        if (keys.some(key => !['after', 'wait_ms'].includes(key)) || new Set(keys).size !== keys.length ||
            !/^(0|[1-9][0-9]*)$/.test(afterText ?? '') || !Number.isSafeInteger(Number(afterText)) || Number(afterText) > bridge.sequence) {
          throw new BridgeError(400, 'Invalid response cursor');
        }
        if (waitText !== null && (!/^(0|[1-9][0-9]*)$/.test(waitText) || !Number.isSafeInteger(Number(waitText)) || Number(waitText) > LONG_POLL_MS)) {
          throw new BridgeError(400, 'Invalid wait_ms');
        }
        const waitMs = waitText === null ? LONG_POLL_MS : Number(waitText);
        bridge.assertHealthy();
        const after = Number(afterText);
        if (bridge.latest && bridge.latest.sequence > after) { json(res, 200, {response: bridge.latest}); return; }
        if (waitMs === 0) { json(res, 200, {response: null}); return; }
        if (waiters >= MAX_WAITERS) throw new BridgeError(429, 'Too many response waiters');
        // Subscription setup is synchronous: no response can land between check and registration.
        waiters++;
        let complete = false;
        const finish = (status, body) => {
          if (complete) return;
          complete = true;
          clearTimeout(timer); bridge.off('change', onChange); res.off('close', onClose); waiters--;
          if (body) json(res, status, body);
        };
        const onClose = () => finish(0, null);
        const onChange = () => {
          if (bridge.failed) finish(503, {error: bridge.failed});
          else if (bridge.latest && bridge.latest.sequence > after) finish(200, {response: bridge.latest});
        };
        const timer = setTimeout(() => finish(200, {response: null}), waitMs);
        bridge.on('change', onChange); res.once('close', onClose);
      } else {
        if (url.search) throw new BridgeError(400, 'Unexpected query');
        if (!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type'] ?? '')) throw new BridgeError(415, 'Expected application/json');
        if (Number(req.headers['content-length'] ?? 0) > MAX_BYTES) throw new BridgeError(413, 'Request body too large');
        if (readers >= MAX_WAITERS) throw new BridgeError(429, 'Too many request bodies');
        readers++;
        let body;
        try { body = await readBody(req); } finally { readers--; }
        if (url.pathname === '/text-complete') {
          json(res, 200, {response: bridge.completeText(body)});
          return;
        }
        await advance(body);
        json(res, 200, {accepted: true});
      }
    } catch (error) {
      const status = error instanceof BridgeError ? error.status : 500;
      if (status === 500) bridge.fail('http_internal_error');
      // Never log request data, IDs, authorization, or exception text.
      log('http_rejected', {status});
      if (!req.complete) res.setHeader('Connection', 'close');
      json(res, status, {error: error instanceof BridgeError ? error.message : 'Internal error'});
    }
  });
  server.maxConnections = 64;
  server.requestTimeout = BODY_TIMEOUT_MS;
  server.headersTimeout = BODY_TIMEOUT_MS;
  server.keepAliveTimeout = 1000;
  server.on('clientError', (_error, socket) => socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'));
  return server;
}
