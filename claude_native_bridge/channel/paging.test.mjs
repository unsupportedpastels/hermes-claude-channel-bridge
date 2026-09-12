import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fixture} from './fixture-support.mjs';

const text = result => result.content[0].text;

test('read_result pages a run-directory spool file and is advertised', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  assert.deepEqual((await f.client.listTools()).tools.map(tool => tool.name), ['respond', 'read_result']);
  const handle = 'r0123456789abcdef0123456789abcdef';
  const payload = 'zero😀one😀two';
  await fs.mkdir(path.join(f.dir, 'spool'), {mode: 0o700});
  await fs.writeFile(path.join(f.dir, 'spool', `${handle}.txt`), payload, {mode: 0o600});

  const page = await f.client.callTool({name: 'read_result', arguments: {handle, offset: 4, length: 5}});
  assert.equal(text(page), '😀one😀');
  assert.equal(page.isError, undefined);
});

test('read_result returns clean tool errors for expired handles and invalid bounds', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  for (const arguments_ of [
    {handle: 'rffffffffffffffffffffffffffffffff', offset: 0, length: 1},
    {handle: '../transport', offset: 0, length: 1},
  ]) {
    const result = await f.client.callTool({name: 'read_result', arguments: arguments_});
    assert.equal(result.isError, true);
    assert.equal(text(result), 'handle expired; re-run the tool');
  }
  for (const arguments_ of [
    {handle: 'r0123456789abcdef0123456789abcdef', offset: -1, length: 1},
    {handle: 'r0123456789abcdef0123456789abcdef', offset: 0, length: 0},
    {handle: 'r0123456789abcdef0123456789abcdef', offset: 0, length: 15001},
  ]) {
    const result = await f.client.callTool({name: 'read_result', arguments: arguments_});
    assert.equal(result.isError, true);
    assert.equal(text(result), 'offset must be nonnegative and length must be from 1 to 15000');
  }
});
