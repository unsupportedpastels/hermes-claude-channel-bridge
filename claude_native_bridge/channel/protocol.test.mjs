import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Bridge, MAX_REQUESTS} from './protocol.mjs';

const request = (request_id, content = 'task') => ({request_id, content});

test('text completion has no held call and exact ack emits another channel input', () => {
  const bridge = new Bridge();
  bridge.advance({ack: null, request: request('a')});
  assert.throws(() => bridge.completeText({request_id: 'wrong', text: 'done'}), /response/);
  assert.deepEqual(bridge.completeText({request_id: 'a', text: 'done'}), {sequence: 1, request_id: 'a', kind: 'final', text: 'done'});
  assert.equal(bridge.held, null);
  assert.throws(() => bridge.completeText({request_id: 'a', text: 'done'}), /response/);
  assert.throws(() => bridge.advance({ack: null, request: request('b')}), /acknowledgement/);
  assert.equal(bridge.advance({ack: 1, request: request('b')}).initial, true);
  const pending = bridge.respond({request_id: 'b', kind: 'tool_calls', tool_calls: [{name: 'x', arguments: {}}]});
  assert.throws(() => bridge.completeText({request_id: 'b', text: 'done'}), /response/);
  assert.equal(bridge.advance({ack: 2, request: request('c')}).initial, false);
  return pending;
});

test('rendezvous publishes a decision but only an exact ack releases its call', async () => {
  const bridge = new Bridge();
  assert.deepEqual(bridge.status(), {sequence: 0, current: null, held: null, failed: null});
  assert.equal(bridge.advance({ack: null, request: request('a')}).initial, true);
  const decision = {request_id: 'a', kind: 'tool_calls', tool_calls: [{name: 'read_file', arguments: {path: '/not-executed'}}]};
  let settled = false;
  const pending = bridge.respond(decision).then(value => { settled = true; return value; });
  await Promise.resolve();
  assert.equal(settled, false);
  assert.deepEqual(bridge.latest, {sequence: 1, ...decision});
  assert.throws(() => bridge.advance({ack: 0, request: request('b')}), /acknowledgement/);
  assert.equal(settled, false);
  assert.equal(bridge.advance({ack: 1, request: request('b')}).initial, false);
  assert.deepEqual(await pending, {request: request('b')});
  assert.equal(bridge.latest, null);
  assert.throws(() => bridge.advance({ack: 1, request: request('c')}), /in.flight/);
  const final = bridge.respond({request_id: 'b', kind: 'final', text: 'done'});
  assert.throws(() => bridge.advance({ack: 2, request: request('a')}), /request ID/);
  bridge.advance({ack: 2, request: request('next-task')});
  assert.deepEqual(await final, {request: request('next-task')});
});

test('native cancellation rejects the held call and permanently breaks the session', async () => {
  const bridge = new Bridge();
  bridge.advance({ack: null, request: request('a')});
  const controller = new AbortController();
  const pending = bridge.respond({request_id: 'a', kind: 'final', text: 'done'}, controller.signal);
  // Observe rejection immediately, even if the assertion below fails.
  const outcome = pending.then(() => 'resolved', error => error.message);
  controller.abort();
  assert.equal(bridge.status().failed, 'native_cancelled');
  assert.equal(await outcome, 'native_cancelled');
  assert.equal(bridge.latest, null);
  assert.throws(() => bridge.advance({ack: 1, request: request('b')}), /native_cancelled/);
  assert.throws(() => bridge.respond({request_id: 'b', kind: 'final', text: 'no'}), /native_cancelled/);
});

test('acknowledgement removes cancellation listener from the old call', async () => {
  const bridge = new Bridge();
  bridge.advance({ack: null, request: request('a')});
  const controller = new AbortController();
  const pending = bridge.respond({request_id: 'a', kind: 'final', text: 'done'}, controller.signal);
  bridge.advance({ack: 1, request: request('b')});
  await pending;
  controller.abort();
  assert.equal(bridge.status().failed, null);
});

test('pre-cancelled call fails without publishing any decision', () => {
  const bridge = new Bridge();
  bridge.advance({ack: null, request: request('a')});
  assert.throws(() => bridge.respond({request_id: 'a', kind: 'final', text: 'no'}, AbortSignal.abort()), /native_cancelled/);
  assert.deepEqual(bridge.status(), {sequence: 0, current: null, held: null, failed: 'native_cancelled'});
  assert.equal(bridge.latest, null);
});

test('finite request budget bounds replay IDs and fails the pending call on exhaustion', async () => {
  const bridge = new Bridge();
  bridge.advance({ack: null, request: request('0')});
  for (let i = 0; i < MAX_REQUESTS; i++) {
    const pending = bridge.respond({request_id: String(i), kind: 'final', text: ''});
    if (i < MAX_REQUESTS - 1) {
      bridge.advance({ack: i + 1, request: request(String(i + 1))});
      await pending;
      assert.equal(bridge.latest, null);
    } else {
      const rejected = assert.rejects(pending, /session_request_limit/);
      assert.throws(() => bridge.advance({ack: i + 1, request: request('overflow')}), /session_request_limit/);
      await rejected;
    }
  }
  assert.equal(bridge.seen.size, MAX_REQUESTS);
  assert.equal(bridge.status().failed, 'session_request_limit');
  assert.equal(bridge.latest, null);
});
