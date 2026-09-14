import {test} from 'node:test';
import assert from 'node:assert/strict';
import {
  JSON_MAX_DEPTH,
  JSON_MAX_NODES,
  MAX_BYTES,
  TOOL_PROPOSAL_MAX_BYTES,
  validateAdvance,
  validateDecision,
} from './protocol.mjs';

const decision = arguments_ => ({request_id: 'r', kind: 'tool_calls', tool_calls: [{name: 'tool', arguments: arguments_}]});

function depth(value) {
  if (value !== null && typeof value === 'object') {
    const items = Array.isArray(value) ? value : Object.values(value);
    return (items.length ? Math.max(...items.map(depth)) : -1) + 1;
  }
  return 0;
}

function nodes(value) {
  if (value !== null && typeof value === 'object') {
    const items = Array.isArray(value) ? value : Object.values(value);
    return 1 + items.reduce((total, item) => total + nodes(item), 0);
  }
  return 1;
}

const encodedBytes = value => Buffer.byteLength(JSON.stringify(value), 'utf8');

test('UTF-8 proposal bytes accept the exact boundary and reject the next codepoint', () => {
  const empty = decision({payload: ''});
  const remaining = TOOL_PROPOSAL_MAX_BYTES - encodedBytes(empty);
  const exact = decision({payload: 'é'.repeat(Math.floor(remaining / 2)) + 'x'.repeat(remaining % 2)});
  assert.equal(encodedBytes(exact), TOOL_PROPOSAL_MAX_BYTES);
  validateDecision(exact);

  exact.tool_calls[0].arguments.payload += 'é';
  assert.throws(() => validateDecision(exact), /too large/);
});

test('proposal depth accepts the boundary and rejects one deeper', () => {
  let arguments_ = {};
  while (depth(decision(arguments_)) < JSON_MAX_DEPTH) arguments_ = {nested: arguments_};
  assert.equal(depth(decision(arguments_)), JSON_MAX_DEPTH);
  validateDecision(decision(arguments_));
  assert.throws(() => validateDecision(decision({nested: arguments_})), /depth/);
});

test('proposal nodes accept the boundary and reject one more', () => {
  const itemCount = JSON_MAX_NODES - nodes(decision({items: []}));
  const exact = decision({items: Array(itemCount).fill(0)});
  assert.equal(nodes(exact), JSON_MAX_NODES);
  validateDecision(exact);
  exact.tool_calls[0].arguments.items.push(0);
  assert.throws(() => validateDecision(exact), /node budget/);
});

test('nonfinite and cyclic proposal values are rejected cleanly', () => {
  for (const value of [NaN, Infinity, -Infinity]) {
    assert.throws(() => validateDecision(decision({value})), /Invalid JSON/);
  }
  const cyclic = {};
  cyclic.self = cyclic;
  assert.throws(() => validateDecision(decision(cyclic)), /cycle/);
});

test('small proposal budget does not cap ordinary request or final text', () => {
  const text = 'é'.repeat(TOOL_PROPOSAL_MAX_BYTES);
  assert.ok(Buffer.byteLength(text) > TOOL_PROPOSAL_MAX_BYTES);
  assert.ok(encodedBytes({request_id: 'r', kind: 'final', text}) < MAX_BYTES);
  validateAdvance({ack: null, request: {request_id: 'r', content: text}});
  validateDecision({request_id: 'r', kind: 'final', text});
});
