import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fixture} from './fixture-support.mjs';

const wake = (request_id, prompt_id, generation, event = 'MessageDisplay') => ({
  request_id, prompt_id, event, generation,
});

test('authenticated wake interrupts the real server long poll without inference', {timeout: 10_000}, async t => {
  const f = await fixture(t);
  await f.advance(null, 'request');
  const started = performance.now();
  const waiting = f.api('/response?after=0&wake_after=0&wait_ms=1000');
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(await f.api('/wake', wake('request', 'prompt', 1)), {
    status: 200, body: {accepted: true},
  });
  assert.deepEqual(await waiting, {
    status: 200,
    body: {response: null, wake: {generation: 1, event: 'MessageDisplay'}},
  });
  assert.ok(performance.now() - started < 300, 'wake notification must avoid the polling interval');
  assert.equal(f.channels.length, 1, 'wake must not issue another model notification');
});

test('pre-poll and skipped generations recover without a lost-wakeup race', {timeout: 10_000}, async t => {
  const f = await fixture(t);
  await f.advance(null, 'request');
  assert.equal((await f.api('/wake', wake('request', 'prompt', 1))).status, 200);
  assert.deepEqual(await f.api('/response?after=0&wake_after=0&wait_ms=1000'), {
    status: 200,
    body: {response: null, wake: {generation: 1, event: 'MessageDisplay'}},
  });
  assert.equal((await f.api('/wake', wake('request', 'prompt', 3, 'Stop'))).status, 200);
  assert.deepEqual(await f.api('/response?after=0&wake_after=1&wait_ms=1000'), {
    status: 200,
    body: {response: null, wake: {generation: 3, event: 'Stop'}},
  });
});

test('wake authentication, shape, request, prompt, and generation fail closed', {timeout: 10_000}, async t => {
  const f = await fixture(t);
  await f.advance(null, 'request');
  assert.equal((await f.api('/wake', wake('request', 'prompt', 1), {auth: ''})).status, 401);
  assert.equal((await f.api('/wake', wake('request', 'prompt', 1))).status, 200);
  for (const body of [
    wake('wrong', 'prompt', 2), wake('request', 'other', 2), wake('request', 'prompt', 0),
    wake('request', 'prompt', 2, 'Other'), {...wake('request', 'prompt', 2), extra: true},
  ]) assert.equal((await f.api('/wake', body)).status, 400);
  assert.equal((await f.api('/wake', wake('request', 'prompt', 1))).status, 409);
  assert.deepEqual(await f.api('/response?after=0&wake_after=0&wait_ms=0'), {
    status: 200,
    body: {response: null, wake: {generation: 1, event: 'MessageDisplay'}},
  });
  assert.equal((await f.api('/response?after=0&wake_after=65537&wait_ms=0')).status, 400);
});
