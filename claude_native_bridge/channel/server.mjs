import fs from 'node:fs';
import path from 'node:path';
import {Server} from '@modelcontextprotocol/sdk/server/index.js';
import {StdioServerTransport} from '@modelcontextprotocol/sdk/server/stdio.js';
import {CallToolRequestSchema, ListToolsRequestSchema, McpError, ErrorCode} from '@modelcontextprotocol/sdk/types.js';
import {Bridge, BridgeError, MAX_BYTES, RESPOND_SCHEMA} from './protocol.mjs';
import {appendDiagnostic} from './diagnostic-log.mjs';
import {createHttpServer} from './http.mjs';
import {assertPrivateFile, readTransport} from './platform.mjs';

const instructions = 'Hermes owns canonical history and task execution. For each authoritative request, answer ordinary text directly and finish normally when no Hermes tool is needed. Do not call respond for an ordinary text final. To propose one to sixteen Hermes tool calls, call respond exactly once with kind tool_calls and the exact request_id. Never execute task tools natively. Any brief pre-tool prose is part of your answer. respond is a yield-and-wait rendezvous: its pending tool result is the NEXT authoritative request. Process that request, then answer directly or propose tools. Do not retry a pending respond call. Use read_result only for paged Hermes tool-result handles. Use read_image with an image handle named in a Hermes placeholder to see that image; you have not seen it until read_image returns it. Cancellation breaks this session.';
const READ_RESULT_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['handle', 'offset', 'length'],
  properties: {
    handle: {type: 'string'},
    offset: {type: 'integer', minimum: 0},
    length: {type: 'integer', minimum: 1, maximum: 15000},
  },
};
const toolText = (text, isError = false) => ({content: [{type: 'text', text}], ...(isError ? {isError: true} : {})});
async function readResult(dir, args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)
      || !/^r[0-9a-f]{32}$/.test(args.handle ?? '')) {
    return toolText('handle expired; re-run the tool', true);
  }
  if (!Number.isSafeInteger(args.offset) || args.offset < 0
      || !Number.isSafeInteger(args.length) || args.length < 1 || args.length > 15000) {
    return toolText('offset must be nonnegative and length must be from 1 to 15000', true);
  }
  try {
    const file = path.join(dir, 'spool', `${args.handle}.txt`);
    const stat = await fs.promises.lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink()) return toolText('handle expired; re-run the tool', true);
    const chars = Array.from(await fs.promises.readFile(file, 'utf8'));
    return toolText(chars.slice(args.offset, args.offset + args.length).join(''));
  } catch {
    return toolText('handle expired; re-run the tool', true);
  }
}
const IMAGE_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['handle'],
  properties: {handle: {type: 'string', pattern: '^i[0-9a-f]{32}$'}},
};
const IMAGE_TYPES = {png: 'image/png', jpg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp'};
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const IMAGE_UNAVAILABLE = 'image handle expired or invalid; the image was not seen';
const imageMatches = (mime, b) => (mime === 'image/png' && b.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')))
  || (mime === 'image/jpeg' && b.subarray(0, 3).equals(Buffer.from('ffd8ff', 'hex')))
  || (mime === 'image/gif' && ['GIF87a', 'GIF89a'].includes(b.subarray(0, 6).toString('latin1')))
  || (mime === 'image/webp' && b.subarray(0, 4).toString('latin1') === 'RIFF' && b.subarray(8, 12).toString('latin1') === 'WEBP');
// Returns the stored image as one MCP image block: never text, never paged. The
// handle is a bare name; it must resolve to a single-link regular file directly
// inside this session's own images directory.
async function readImage(dir, args) {
  if (!args || typeof args !== 'object' || Array.isArray(args) || Object.keys(args).length !== 1
      || typeof args.handle !== 'string' || !/^i[0-9a-f]{32}$/.test(args.handle)) {
    return toolText(IMAGE_UNAVAILABLE, true);
  }
  try {
    const root = path.join(dir, 'images');
    const rootStat = await fs.promises.lstat(root);
    if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) return toolText(IMAGE_UNAVAILABLE, true);
    const realRoot = await fs.promises.realpath(root);
    for (const [extension, mimeType] of Object.entries(IMAGE_TYPES)) {
      const file = path.join(root, `${args.handle}.${extension}`);
      let stat;
      try { stat = await fs.promises.lstat(file); } catch (error) { if (error.code === 'ENOENT') continue; throw error; }
      if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size < 1 || stat.size > MAX_IMAGE_BYTES) {
        return toolText(IMAGE_UNAVAILABLE, true);
      }
      assertPrivateFile(file); // owner-only + no reparse/link: POSIX mode/uid, Windows ACL
      const real = await fs.promises.realpath(file);
      if (path.dirname(real) !== realRoot) return toolText(IMAGE_UNAVAILABLE, true);
      const handle = await fs.promises.open(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
      try {
        const opened = await handle.stat();
        if (!opened.isFile() || opened.nlink !== 1 || opened.size !== stat.size
            || (process.platform !== 'win32' && (opened.ino !== stat.ino || opened.dev !== stat.dev))) {
          return toolText(IMAGE_UNAVAILABLE, true);
        }
        const bytes = Buffer.alloc(opened.size);
        let offset = 0;
        while (offset < bytes.length) {
          const {bytesRead} = await handle.read(bytes, offset, bytes.length - offset, offset);
          if (bytesRead === 0) break;
          offset += bytesRead;
        }
        if (offset !== bytes.length || !imageMatches(mimeType, bytes)) return toolText(IMAGE_UNAVAILABLE, true);
        return {content: [{type: 'image', data: bytes.toString('base64'), mimeType}]};
      } finally { await handle.close(); }
    }
  } catch {}
  return toolText(IMAGE_UNAVAILABLE, true);
}
// Diagnostics stay inside the private session runtime: a native CLI persists an MCP
// server's stderr in its own log, which is neither a private nor a temporary
// location, so sequences and process metadata must never be written there.
const log = (event, metadata = {}) => {
  if (process.env.HERMES_BRIDGE_DIAGNOSTICS !== '1') return;
  const dir = process.env.HERMES_BRIDGE_RUNTIME_DIR;
  if (!dir) return;
  try {
    appendDiagnostic(dir, event, metadata);
  } catch {}
};

let mcp, httpServer, bridge, readyFile, tempFile, lockFile;
let ownsReady = false, ownsLock = false, ownsTemp = false, stopping = false;
function removeOwnedFiles() {
  for (const file of [ownsReady && readyFile, ownsTemp && tempFile, ownsLock && lockFile]) {
    if (file) { try { fs.unlinkSync(file); } catch {} }
  }
  ownsReady = false; ownsLock = false; ownsTemp = false;
}
async function shutdown(reason, code = 0) {
  if (stopping) return;
  stopping = true;
  bridge?.fail(reason);
  log('stopping', {reason});
  removeOwnedFiles();
  // Hard deadline also covers a native peer which stopped reading stdout.
  const deadline = setTimeout(() => process.exit(code), 1000);
  deadline.unref();
  httpServer?.close();
  httpServer?.closeAllConnections();
  try { await mcp?.close(); } catch {}
  process.exit(code);
}
process.once('SIGTERM', () => void shutdown('terminated'));
process.once('SIGINT', () => void shutdown('terminated'));
process.stdin.once('end', () => void shutdown('stdin_closed'));
process.stdin.once('close', () => void shutdown('stdin_closed'));
process.stdout.once('error', () => void shutdown('stdio_error', 1));
process.once('exit', removeOwnedFiles);

try {
  const dir = process.env.HERMES_BRIDGE_RUNTIME_DIR;
  const config = readTransport(dir);
  if (!config || Object.keys(config).length !== 1 || typeof config.token !== 'string' || !/^[\x21-\x7e]{1,4096}$/.test(config.token)) throw new Error('transport');
  readyFile = path.join(dir, 'ready.json');
  if (fs.existsSync(readyFile)) throw new Error('existing ready file');
  lockFile = path.join(dir, 'bridge.lock');
  fs.closeSync(fs.openSync(lockFile, 'wx', 0o600));
  ownsLock = true;

  bridge = new Bridge();
  mcp = new Server({name: 'hermesbridge', version: '0.1.0'}, {
    capabilities: {experimental: {'claude/channel': {}}, tools: {}}, instructions,
  });
  mcp.setRequestHandler(ListToolsRequestSchema, async () => ({tools: [
    {
      name: 'respond', description: 'Propose Hermes tool calls ONLY when task tools are needed, then wait for the next authoritative request. For a final answer use ordinary assistant text instead of this tool. Never execute proposed task tools.', inputSchema: RESPOND_SCHEMA,
    },
    {
      name: 'read_result', description: 'Read a page from an oversized Hermes tool result using the handle from its compact result envelope.', inputSchema: READ_RESULT_SCHEMA,
    },
    {
      name: 'read_image', description: 'View an image Hermes attached, using the handle named in its placeholder. Returns the image itself; the image is not seen until this succeeds.', inputSchema: IMAGE_SCHEMA,
    },
  ]}));
  mcp.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
    if (request.params.name === 'read_result') return readResult(dir, request.params.arguments);
    if (request.params.name === 'read_image') return readImage(dir, request.params.arguments);
    if (request.params.name !== 'respond') throw new McpError(ErrorCode.InvalidParams, 'Only respond, read_result and read_image are supported');
    try {
      const pending = bridge.respond(request.params.arguments, extra.signal);
      log('decision', {sequence: bridge.sequence, kind: request.params.arguments.kind});
      return {content: [{type: 'text', text: JSON.stringify(await pending)}]};
    } catch (error) {
      if (error instanceof BridgeError && error.diagnostic) {
        log('proposal_rejected', {sequence: bridge.sequence, ...error.diagnostic});
      }
      throw new McpError(error instanceof BridgeError && error.status < 500 ? ErrorCode.InvalidParams : ErrorCode.InternalError,
        error instanceof BridgeError ? error.message : 'Bridge response failed');
    }
  });
  mcp.onerror = () => { bridge.fail('native_transport_error'); log('native_error'); };
  mcp.onclose = () => void shutdown('native_closed', 1);
  let initialized = false;
  httpServer = createHttpServer({bridge, token: config.token, log, advance: async body => {
    if (!initialized) throw new BridgeError(503, 'Native MCP not initialized');
    const next = bridge.advance(body);
    if (next.initial) {
      try { await mcp.notification({method: 'notifications/claude/channel', params: {content: JSON.stringify({request: next.request}), meta: {request_id: next.request.request_id}}}); }
      catch { bridge.fail('notification_failed'); throw new BridgeError(503, 'notification_failed'); }
    }
    log('advance', {sequence: bridge.sequence, initial: next.initial});
  }});
  httpServer.on('error', () => void shutdown('http_server_error', 1));
  await new Promise((resolve, reject) => { httpServer.once('error', reject); httpServer.listen(0, '127.0.0.1', resolve); });
  mcp.oninitialized = () => {
    if (initialized || stopping) return;
    initialized = true;
    try {
      tempFile = path.join(dir, `.ready-${process.pid}.tmp`);
      const readyFd = fs.openSync(tempFile, 'wx', 0o600);
      ownsTemp = true;
      try {
        fs.writeFileSync(readyFd, JSON.stringify({port: httpServer.address().port, pid: process.pid}));
        fs.fsyncSync(readyFd);
      } finally { fs.closeSync(readyFd); }
      fs.renameSync(tempFile, readyFile);
      tempFile = null; ownsTemp = false; ownsReady = true;
      log('ready', {port: httpServer.address().port, pid: process.pid});
    } catch { void shutdown('ready_write_failed', 1); }
  };
  await mcp.connect(new StdioServerTransport(process.stdin, process.stdout, {maxBufferSize: MAX_BYTES}));
} catch {
  // Startup errors deliberately omit config values, paths, and token material.
  process.stderr.write('hermesbridge: startup failed; check private runtime directory and transport configuration\n');
  await shutdown('startup_failed', 1);
}
