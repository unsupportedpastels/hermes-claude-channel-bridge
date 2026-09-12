import {test} from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {setTimeout as delay} from 'node:timers/promises';
import {fixture} from './fixture-support.mjs';
import {MAX_BYTES} from './protocol.mjs';

const final = (request_id, text = 'done') => ({request_id, kind: 'final', text});

test('every HTTP route authenticates before dispatch; invalid bodies do not mutate state', {timeout: 15_000}, async t => {
  const f = await fixture(t, {diagnostics: true});
  for (const endpoint of ['/status', '/advance', '/response?after=0', '/missing']) {
    for (const auth of ['', 'Bearer x', `Bearer ${'x'.repeat(f.token.length)}`, `Bearer ${f.token}x`]) {
      assert.equal((await f.api(endpoint, undefined, {auth})).status, 401);
    }
  }
  for (const [endpoint, method, expected] of [
    ['/status', 'POST', 405], ['/advance', 'GET', 405], ['/response?after=0', 'POST', 405],
    ['/status', 'OPTIONS', 405], ['/missing', 'GET', 404], ['/status?bad=1', 'GET', 400],
  ]) assert.equal((await f.api(endpoint, undefined, {method})).status, expected);
  for (const raw of ['{', 'null', '[]', '{}', '{"ack":null}', '{"ack":null,"request":{}}']) {
    assert.equal((await f.api('/advance', undefined, {method: 'POST', raw})).status, 400, raw);
  }
  const valid = {ack: null, request: {request_id: 'a', content: 'sensitive prompt must not be logged'}};
  for (const body of [
    {...valid, extra: true}, {...valid, ack: '1'}, {...valid, ack: -1}, {...valid, ack: 1.5},
    {...valid, request: {...valid.request, extra: true}}, {...valid, request: {request_id: '', content: ''}},
    {...valid, request: {request_id: 'a', content: 42}}, {...valid, request: {request_id: 'a'.repeat(257), content: ''}},
  ]) assert.equal((await f.api('/advance', body)).status, 400);
  assert.equal((await f.api('/advance', valid, {headers: {'Content-Type': 'text/plain'}})).status, 415);
  for (const query of ['', '?after=', '?after=-1', '?after=0.5', '?after=1', '?after=NaN', '?after=9007199254740992']) {
    assert.equal((await f.api(`/response${query}`)).status, 400, query);
  }
  assert.deepEqual((await f.api('/status')).body, {sequence: 0, current: null, held: null, failed: null});
  assert.equal((await f.api('/advance', valid)).status, 200);
  const pending = f.respond(final('a', 'sensitive final must not be logged'));
  await f.api('/response?after=0');
  await f.advance(1, 'b');
  await pending;
  await f.client.close();
  const text = f.logs.join('');
  assert.ok(text.includes('http_rejected'));
  for (const forbidden of [f.token, valid.request.content, 'sensitive final must not be logged']) assert.ok(!text.includes(forbidden));
  for (const line of text.trim().split('\n')) {
    const event = JSON.parse(line);
    assert.ok(!Number.isNaN(Date.parse(event.time)));
    assert.ok(Object.keys(event).every(key => ['time', 'event', 'status', 'sequence', 'kind', 'initial', 'port', 'pid', 'reason'].includes(key)));
  }
});

test('SDK respond validates exact union, correlation, call-count and nested keys', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  await assert.rejects(f.client.callTool({name: 'execute', arguments: {}}), /Only respond/);
  await f.advance(null, 'a');
  const call = {name: 'proposed_only', arguments: {nested: [1, null, {anything: true}]}};
  for (const body of [
    {}, final('wrong'), {...final('a'), tool_calls: [call]}, {...final('a'), extra: 1},
    {request_id: 'a', kind: 'final'}, {request_id: 'a', kind: 'final', text: null},
    {request_id: 'a', kind: 'tool_call', name: 'old', arguments: {}},
    {request_id: 'a', kind: 'tool_calls', tool_calls: []},
    {request_id: 'a', kind: 'tool_calls', tool_calls: Array(17).fill(call)},
    {request_id: 'a', kind: 'tool_calls', tool_calls: [{...call, extra: 1}]},
    {request_id: 'a', kind: 'tool_calls', tool_calls: [{name: '', arguments: {}}]},
    {request_id: 'a', kind: 'tool_calls', tool_calls: [{name: 'x', arguments: []}]},
    {request_id: 'a', kind: 'tool_calls', tool_calls: [{name: 'x', arguments: null}]},
  ]) {
    await assert.rejects(f.respond(body));
    assert.deepEqual((await f.api('/status')).body, {sequence: 0, current: 'a', held: null, failed: null});
  }
  const pending = f.respond({request_id: 'a', kind: 'tool_calls', tool_calls: Array(16).fill(call)});
  assert.equal((await f.api('/response?after=0')).body.response.tool_calls.length, 16);
  await f.advance(1, 'b');
  await pending;
});

function chunkedPost(f, chunks) {
  return new Promise((resolve, reject) => {
    const req = http.request({host: '127.0.0.1', port: f.ready.port, path: '/advance', method: 'POST',
      headers: {Authorization: `Bearer ${f.token}`, 'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked'},
    }, res => {
      const data = [];
      res.on('data', chunk => data.push(chunk));
      res.on('end', () => resolve({status: res.statusCode, body: JSON.parse(Buffer.concat(data))}));
    });
    req.on('error', reject);
    req.setTimeout(10_000, () => req.destroy(new Error('chunked test timeout')));
    for (const chunk of chunks) req.write(chunk);
    req.end();
  });
}

test('8MiB byte limit covers declared and chunked bodies, including multibyte UTF-8', {timeout: 20_000}, async t => {
  const f = await fixture(t);
  const oversized = JSON.stringify({ack: null, request: {request_id: 'a', content: 'é'.repeat(MAX_BYTES / 2)}});
  assert.ok(oversized.length < MAX_BYTES && Buffer.byteLength(oversized) > MAX_BYTES);
  assert.equal((await f.api('/advance', undefined, {method: 'POST', raw: oversized})).status, 413);
  const chunked = await chunkedPost(f, Array(9).fill(Buffer.alloc(1024 * 1024, ' ')));
  assert.equal(chunked.status, 413);
  assert.deepEqual((await f.api('/status')).body, {sequence: 0, current: null, held: null, failed: null});
  const prefix = '{"ack":null,"request":{"request_id":"a","content":"';
  const suffix = '"}}';
  const exact = prefix + 'x'.repeat(MAX_BYTES - Buffer.byteLength(prefix + suffix)) + suffix;
  assert.equal(Buffer.byteLength(exact), MAX_BYTES);
  assert.equal((await f.api('/advance', undefined, {method: 'POST', raw: exact})).status, 200);
});

test('default empty long-poll returns null after ten seconds', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  const start = Date.now();
  assert.deepEqual(await f.api('/response?after=0'), {status: 200, body: {response: null}});
  const elapsed = Date.now() - start;
  assert.ok(elapsed >= 9900 && elapsed < 12_000, `unexpected long-poll duration ${elapsed}`);
});

test('parallel advance accepts only one request and disconnected polls release capacity', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  const outcomes = await Promise.all([f.advance(null, 'a'), f.advance(null, 'b')]);
  assert.deepEqual(outcomes.map(value => value.status).sort(), [200, 409]);
  const controllers = Array.from({length: 32}, () => new AbortController());
  const polls = controllers.map(controller => f.api('/response?after=0', undefined, {signal: controller.signal}).catch(() => null));
  await delay(100);
  assert.equal((await f.api('/response?after=0&wait_ms=10')).status, 429);
  controllers.forEach(controller => controller.abort());
  await Promise.all(polls);
  await delay(100);
  assert.deepEqual(await f.api('/response?after=0&wait_ms=10'), {status: 200, body: {response: null}});
});
