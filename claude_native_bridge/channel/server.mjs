import fs from 'node:fs';
import path from 'node:path';
import {Server} from '@modelcontextprotocol/sdk/server/index.js';
import {StdioServerTransport} from '@modelcontextprotocol/sdk/server/stdio.js';
import {CallToolRequestSchema, ListToolsRequestSchema, McpError, ErrorCode} from '@modelcontextprotocol/sdk/types.js';
import {Bridge, BridgeError, MAX_BYTES, RESPOND_SCHEMA} from './protocol.mjs';
import {createHttpServer} from './http.mjs';
import {readTransport} from './platform.mjs';

const instructions = 'Hermes owns canonical history and task execution. For each authoritative request, answer ordinary text directly and finish normally when no Hermes tool is needed. Do not call respond for an ordinary text final. To propose one to sixteen Hermes tool calls, call respond exactly once with kind tool_calls and the exact request_id. Never execute task tools natively. Any brief pre-tool prose is part of your answer. respond is a yield-and-wait rendezvous: its pending tool result is the NEXT authoritative request. Process that request, then answer directly or propose tools. Do not retry a pending respond call. Cancellation breaks this session.';
const log = (event, metadata = {}) => {
  if (process.env.HERMES_BRIDGE_DIAGNOSTICS === '1') process.stderr.write(JSON.stringify({time: new Date().toISOString(), event, ...metadata}) + '\n');
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
  mcp.setRequestHandler(ListToolsRequestSchema, async () => ({tools: [{
    name: 'respond', description: 'Propose Hermes tool calls ONLY when task tools are needed, then wait for the next authoritative request. For a final answer use ordinary assistant text instead of this tool. Never execute proposed task tools.', inputSchema: RESPOND_SCHEMA,
  }]}));
  mcp.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
    if (request.params.name !== 'respond') throw new McpError(ErrorCode.InvalidParams, 'Only respond is supported');
    try {
      const pending = bridge.respond(request.params.arguments, extra.signal);
      log('decision', {sequence: bridge.sequence, kind: request.params.arguments.kind});
      return {content: [{type: 'text', text: JSON.stringify(await pending)}]};
    } catch (error) {
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
