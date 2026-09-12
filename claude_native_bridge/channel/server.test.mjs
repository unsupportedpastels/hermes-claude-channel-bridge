import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {fixture, until} from './fixture-support.mjs';

const final = (request_id, text = 'done') => ({request_id, kind: 'final', text});

test('authenticated text completion retains MCP lifecycle and sends next channel notification', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  await f.advance(null, 'a');
  await until(() => f.channels.length === 1);
  const body = {request_id: 'a', text: 'ordinary text'};
  assert.equal((await f.api('/text-complete', body, {auth: 'Bearer wrong'})).status, 401);
  assert.equal((await f.api('/status')).body.current, 'a');
  assert.deepEqual((await f.api('/text-complete', body)).body.response, {sequence: 1, ...final('a', body.text)});
  assert.deepEqual((await f.api('/status')).body, {sequence: 1, current: null, held: null, failed: null});
  assert.equal((await f.api('/text-complete', body)).status, 409);
  assert.equal((await f.advance(null, 'b')).status, 409);
  assert.equal((await f.advance(1, 'b')).status, 200);
  await until(() => f.channels.length === 2);
  assert.equal(f.channels[1].meta.request_id, 'b');
  assert.equal(f.ready.pid, f.transport.pid);
  const pending = f.respond({request_id: 'b', kind: 'tool_calls', tool_calls: [{name: 'x', arguments: {}}]});
  await f.api('/response?after=1');
  assert.equal((await f.api('/text-complete', {request_id: 'b', text: 'wrong'})).status, 409);
  await f.advance(2, 'c');
  await pending;
  assert.equal(f.channels.length, 2);
});
const result = response => JSON.parse(response.content[0].text);

test('SDK handshake, channel delivery, held rendezvous and next task after final', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  assert.equal(f.client.getServerVersion().name, 'hermesbridge');
  assert.deepEqual(f.client.getServerCapabilities().experimental, {'claude/channel': {}});
  const tools = (await f.client.listTools()).tools;
  assert.deepEqual(tools.map(tool => tool.name), ['respond']);
  assert.equal(tools[0].inputSchema.additionalProperties, false);
  assert.deepEqual(tools[0].inputSchema.properties.kind.enum, ['tool_calls']);
  assert.equal((await fs.stat(path.join(f.dir, 'ready.json'))).mode & 0o777, 0o600);
  assert.equal(f.ready.pid, f.transport.pid);
  assert.deepEqual((await f.api('/status')).body, {sequence: 0, current: null, held: null, failed: null});
  assert.equal((await f.advance(null, 'a', 'start')).status, 200);
  await until(() => f.channels.length === 1);
  assert.deepEqual(JSON.parse(f.channels[0].content), {request: {request_id: 'a', content: 'start'}});
  assert.deepEqual(f.channels[0].meta, {request_id: 'a'});
  assert.equal((await f.advance(null, 'overlap')).status, 409);

  const decision = {request_id: 'a', kind: 'tool_calls', tool_calls: [{name: 'never_executed', arguments: {value: 1}}]};
  let settled = false;
  const pending = f.respond(decision).then(value => { settled = true; return value; });
  pending.catch(() => {});
  const published = await f.api('/response?after=0');
  assert.deepEqual(published, {status: 200, body: {response: {sequence: 1, ...decision}}});
  await delay(40);
  assert.equal(settled, false, 'respond must remain pending after publication');
  assert.deepEqual((await f.api('/status')).body, {sequence: 1, current: null, held: 1, failed: null});
  assert.equal((await f.advance(0, 'b')).status, 409);
  assert.equal((await f.advance(null, 'b')).status, 409);
  assert.equal((await f.advance(1, 'a')).status, 409);
  await assert.rejects(f.respond(final('a')), /overlapping/);
  assert.equal(settled, false);
  assert.equal((await f.advance(1, 'b', 'tool output')).status, 200);
  assert.deepEqual(result(await pending), {request: {request_id: 'b', content: 'tool output'}});
  assert.equal((await f.advance(1, 'b')).status, 409);

  const pendingFinal = f.respond(final('b'));
  assert.equal((await f.api('/response?after=1')).body.response.sequence, 2);
  assert.equal((await f.advance(1, 'next')).status, 409);
  assert.equal((await f.advance(2, 'a')).status, 409);
  assert.equal((await f.advance(2, 'next', 'new task')).status, 200);
  assert.deepEqual(result(await pendingFinal), {request: {request_id: 'next', content: 'new task'}});
  assert.equal(f.channels.length, 1, 'subsequent requests travel only in held tool results');
  assert.equal(f.logs.join(''), '', 'diagnostics are opt-in');
});

test('response wait_ms is bounded, validates values, and zero wait discards acknowledged history', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  const start = Date.now();
  assert.deepEqual(await f.api('/response?after=0&wait_ms=500'), {status: 200, body: {response: null}});
  assert.ok(Date.now() - start >= 450, '500ms wait should not return immediately');
  assert.ok(Date.now() - start < 3000, '500ms wait must not block for the 10s default');
  for (const value of ['-1', '10001', '1.5', 'abc', '', 'Infinity', '00']) {
    assert.equal((await f.api(`/response?after=0&wait_ms=${value}`)).status, 400, value);
  }
  for (const query of ['after=0&wait_ms=1&wait_ms=2', 'after=0&extra=1', 'after=0&after=0']) {
    assert.equal((await f.api(`/response?${query}`)).status, 400, query);
  }
  assert.equal((await f.advance(null, 'a')).status, 200);
  const pending = f.respond(final('a'));
  assert.equal((await f.api('/response?after=0')).body.response.sequence, 1);
  assert.equal((await f.advance(1, 'b')).status, 200);
  await pending;
  assert.deepEqual(await f.api('/response?after=0&wait_ms=0'), {status: 200, body: {response: null}});
});

test('real SDK cancellation fails closed and wakes HTTP long-poll callers', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  await f.advance(null, 'a');
  const controller = new AbortController();
  const pending = f.respond(final('a'), {signal: controller.signal});
  assert.equal((await f.api('/response?after=0')).body.response.sequence, 1);
  const waiting = f.api('/response?after=1');
  await delay(30);
  controller.abort(new Error('offline cancellation'));
  await assert.rejects(pending, /offline cancellation/);
  assert.deepEqual(await waiting, {status: 503, body: {error: 'native_cancelled'}});
  assert.deepEqual((await f.api('/status')).body, {sequence: 1, current: null, held: null, failed: 'native_cancelled'});
  assert.equal((await f.advance(1, 'b')).status, 503);
  await assert.rejects(f.respond(final('b')), /native_cancelled/);
});

test('waiting HTTP response is released by a later SDK decision', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  await f.advance(null, 'a');
  let settled = false;
  const waiting = f.api('/response?after=0&wait_ms=10000').then(value => {settled = true; return value;});
  await delay(50);
  assert.equal(settled, false);
  const pending = f.respond(final('a', 'published later'));
  assert.deepEqual((await waiting).body.response, {sequence: 1, ...final('a', 'published later')});
  await f.advance(1, 'b');
  await pending;
});
