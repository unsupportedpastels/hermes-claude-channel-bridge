import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {fixture} from './fixture-support.mjs';

const handle = seed => 'i' + seed.padStart(32, '0');
const PNG = Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), Buffer.from('png-body')]);
const JPEG = Buffer.concat([Buffer.from('ffd8ff', 'hex'), Buffer.from('jpeg-body')]);
const GIF = Buffer.from('GIF89a-body');
const WEBP = Buffer.concat([Buffer.from('RIFF'), Buffer.from([1, 2, 3, 4]), Buffer.from('WEBPbody')]);
const UNAVAILABLE = 'image handle expired or invalid; the image was not seen';

async function put(f, name, bytes) {
  await fs.mkdir(path.join(f.dir, 'images'), {mode: 0o700, recursive: true});
  const file = path.join(f.dir, 'images', name);
  await fs.writeFile(file, bytes, {mode: 0o600});
  return file;
}
const read = (f, value) => f.client.callTool({name: 'read_image', arguments: {handle: value}});
const assertUnavailable = result => {
  assert.equal(result.isError, true);
  assert.deepEqual(result.content, [{type: 'text', text: UNAVAILABLE}]);
};
async function trySymlink(target, link) {
  try { await fs.symlink(target, link); return true; } catch (error) {
    if (process.platform === 'win32' && ['EPERM', 'EACCES'].includes(error.code)) return false;
    throw error;
  }
}

test('read_image is advertised with a strict handle-only schema', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  const tool = (await f.client.listTools()).tools.find(entry => entry.name === 'read_image');
  assert.ok(tool);
  assert.equal(tool.inputSchema.additionalProperties, false);
  assert.deepEqual(tool.inputSchema.required, ['handle']);
  assert.deepEqual(Object.keys(tool.inputSchema.properties), ['handle']);
  assert.match(f.client.getInstructions(), /read_image/);
});

test('read_image returns each supported type as one intact MCP image block, never text', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  const cases = [['png', 'image/png', PNG], ['jpg', 'image/jpeg', JPEG], ['gif', 'image/gif', GIF], ['webp', 'image/webp', WEBP]];
  for (const [index, [extension, mimeType, bytes]] of cases.entries()) {
    await put(f, `${handle(String(index))}.${extension}`, bytes);
  }
  // Ordered reads: each handle returns its own bytes regardless of read order.
  for (const index of [3, 0, 2, 1]) {
    const [, mimeType, bytes] = cases[index];
    const result = await read(f, handle(String(index)));
    assert.equal(result.isError, undefined);
    assert.equal(result.content.length, 1);
    assert.deepEqual(result.content[0], {type: 'image', data: bytes.toString('base64'), mimeType});
  }
});

test('read_image returns a multi-megabyte image whole, not in pages', {timeout: 30_000}, async t => {
  const f = await fixture(t);
  const big = Buffer.concat([PNG, Buffer.alloc(4 * 1024 * 1024, 7)]);
  await put(f, `${handle('b')}.png`, big);
  const result = await read(f, handle('b'));
  assert.equal(result.content.length, 1);
  assert.equal(result.content[0].type, 'image');
  assert.equal(Buffer.from(result.content[0].data, 'base64').equals(big), true);
});

test('read_image rejects malformed handles, traversal and extra arguments', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  await fs.writeFile(path.join(f.dir, 'secret.png'), PNG);
  await put(f, `${handle('1')}.png`, PNG);
  for (const value of ['../secret', '../images/' + handle('1'), `${handle('1')}.png`, handle('1').toUpperCase(),
      'i1234', handle('1') + '0', '', 'r' + '0'.repeat(32), `${handle('1')}/../${handle('1')}`, '..\\secret']) {
    assertUnavailable(await read(f, value));
  }
  assertUnavailable(await f.client.callTool({name: 'read_image', arguments: {handle: handle('1'), path: '/etc/passwd'}}));
  assertUnavailable(await f.client.callTool({name: 'read_image', arguments: {}}));
  assertUnavailable(await f.client.callTool({name: 'read_image', arguments: {handle: 7}}));
});

test('read_image refuses symlink and hardlink escapes and a symlinked images directory', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  const outside = path.join(f.dir, 'outside.png');
  await fs.writeFile(outside, PNG);
  await fs.mkdir(path.join(f.dir, 'images'), {mode: 0o700});
  if (await trySymlink(outside, path.join(f.dir, 'images', `${handle('a')}.png`))) {
    assertUnavailable(await read(f, handle('a')));
  }
  await fs.link(outside, path.join(f.dir, 'images', `${handle('c')}.png`));
  assertUnavailable(await read(f, handle('c')));
  await fs.mkdir(path.join(f.dir, 'images', `${handle('d')}.png`));
  assertUnavailable(await read(f, handle('d')));

  await fs.rm(path.join(f.dir, 'images'), {recursive: true, force: true});
  const elsewhere = path.join(f.dir, 'elsewhere');
  await fs.mkdir(elsewhere);
  await fs.writeFile(path.join(elsewhere, `${handle('e')}.png`), PNG);
  if (await trySymlink(elsewhere, path.join(f.dir, 'images'), process.platform === 'win32' ? 'junction' : 'dir')) {
    assertUnavailable(await read(f, handle('e')));
  }
});

test('read_image rejects mismatched, empty and oversize files and missing handles', {timeout: 15_000}, async t => {
  const f = await fixture(t);
  await put(f, `${handle('1')}.png`, JPEG);
  await put(f, `${handle('2')}.png`, Buffer.alloc(0));
  await put(f, `${handle('3')}.png`, Buffer.concat([PNG, Buffer.alloc(5 * 1024 * 1024)]));
  for (const seed of ['1', '2', '3', '4']) assertUnavailable(await read(f, handle(seed)));
});

test('read_image reads only its own session directory and stops at deletion', {timeout: 30_000}, async t => {
  const first = await fixture(t);
  const second = await fixture(t);
  const file = await put(first, `${handle('9')}.png`, PNG);
  assert.equal((await read(first, handle('9'))).content[0].type, 'image');
  assertUnavailable(await read(second, handle('9')));
  await fs.rm(file);
  assertUnavailable(await read(first, handle('9')));
});

test('read_image refuses a publicly readable or foreign-ACL image file', {timeout: 30_000}, async t => {
  const f = await fixture(t);
  const file = await put(f, `${handle('f')}.png`, PNG);
  assert.equal((await read(f, handle('f'))).content[0].type, 'image');
  if (process.platform === 'win32') {
    execFileSync('icacls', [file, '/grant', '*S-1-1-0:R'], {stdio: 'pipe'});
  } else {
    await fs.chmod(file, 0o644);
  }
  assertUnavailable(await read(f, handle('f')));
});

test('read_image refuses a group-writable file on POSIX', {timeout: 15_000, skip: process.platform === 'win32'}, async t => {
  const f = await fixture(t);
  const file = await put(f, `${handle('9')}.png`, PNG);
  await fs.chmod(file, 0o620);
  assertUnavailable(await read(f, handle('9')));
});
