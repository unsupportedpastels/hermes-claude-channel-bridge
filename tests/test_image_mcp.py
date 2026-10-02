"""Python-written image store read back through the real channel MCP server.

Drives channel/server.mjs over stdio with the SDK client (Node), so the handle in
the bridge's placeholder, the store layout and the MCP image block are verified
end to end without a native Claude process.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from claude_native_bridge.images import externalize_images

from image_fixtures import GIF, JPEG, PNG, image_part

CHANNEL = Path(__file__).resolve().parents[1] / "claude_native_bridge" / "channel"
pytestmark = pytest.mark.skipif(
    shutil.which("node") is None
    or not (CHANNEL / "node_modules").exists(),
    reason="needs node and installed channel dependencies",
)

DRIVER = """
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
const [dir, server, ...handles] = process.argv.slice(1);
const transport = new StdioClientTransport({command: process.execPath, args: [server],
  env: {...process.env, HERMES_BRIDGE_RUNTIME_DIR: dir}, stderr: 'pipe'});
const client = new Client({name: 'py-fixture', version: '1'}, {capabilities: {}});
await client.connect(transport);
const out = [];
for (const handle of handles) out.push(await client.callTool({name: 'read_image', arguments: {handle}}));
console.log(JSON.stringify(out));
await client.close();
"""


@pytest.fixture
def runtime(tmp_path):
    """Empty private runtime directory, as the supervisor creates it."""
    path = tmp_path.resolve() / "runtime"  # resolved: macOS /var -> /private/var
    if sys.platform == "win32":
        from claude_native_bridge.windows_security import secure_runtime_directory

        return secure_runtime_directory(path)
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def read_images(runtime, handles):
    fd = os.open(runtime / "transport.json", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as transport:
        json.dump({"token": "t" * 32}, transport)
    done = subprocess.run(
        ["node", "--input-type=module", "-e", DRIVER, str(runtime), str(CHANNEL / "server.mjs"), *handles],
        cwd=CHANNEL, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_placeholder_handles_resolve_to_intact_image_blocks_in_order(runtime):
    messages = [
        {"role": "user", "content": [image_part(PNG), image_part(JPEG, "image/jpeg")]},
        {"role": "tool", "tool_call_id": "c", "content": [image_part(GIF, "image/gif")]},
    ]
    native = externalize_images(messages, runtime)
    placeholders = [
        part["text"] for message in native for part in message["content"]
    ]
    handles = [re.search(r'handle="(i[0-9a-f]{32})"', text).group(1) for text in placeholders]

    results = read_images(runtime, handles + ["i" + "0" * 32])

    expected = [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif")]
    for result, (data, mime) in zip(results, expected):
        assert result["content"] == [
            {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime}
        ]
    assert results[3]["isError"] is True
    assert "not seen" in results[3]["content"][0]["text"]


def test_handle_for_an_image_dropped_by_history_is_unavailable(runtime):
    first = externalize_images([{"role": "user", "content": [image_part(PNG)]}], runtime)
    handle = re.search(r'handle="(i[0-9a-f]{32})"', first[0]["content"][0]["text"]).group(1)
    externalize_images([{"role": "user", "content": "images stripped"}], runtime)

    (result,) = read_images(runtime, [handle])

    assert result["isError"] is True


def _stored(runtime, data=PNG):
    native = externalize_images([{"role": "user", "content": [image_part(data)]}], runtime)
    handle = re.search(r'handle="(i[0-9a-f]{32})"', native[0]["content"][0]["text"]).group(1)
    (file,) = (runtime / "images").iterdir()
    return handle, file


def _make_public(file):
    if sys.platform == "win32":
        # Grant Everyone (well-known SID) read access: owner-only no longer holds.
        subprocess.run(["icacls", str(file), "/grant", "*S-1-1-0:R"], check=True, capture_output=True)
    else:
        file.chmod(0o644)


def test_publicly_readable_image_file_is_refused(runtime):
    handle, file = _stored(runtime)
    (ok,) = read_images(runtime, [handle])
    assert ok["content"][0]["type"] == "image"
    _make_public(file)
    (result,) = read_images(runtime, [handle])
    assert result["isError"] is True and "not seen" in result["content"][0]["text"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership is mode based")
def test_group_writable_images_file_and_hardlink_are_refused(runtime, tmp_path):
    handle, file = _stored(runtime)
    outside = tmp_path / "outside.png"
    file.rename(outside)
    os.link(outside, file)
    (result,) = read_images(runtime, [handle])
    assert result["isError"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL/reparse behavior")
def test_windows_images_inherit_owner_only_acl_and_reject_reparse_points(runtime, tmp_path):
    from claude_native_bridge.windows_security import assert_private_file

    handle, file = _stored(runtime)
    assert_private_file(file)  # inherited owner-only ACL from the protected runtime
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / file.name).write_bytes(PNG)
    shutil.rmtree(runtime / "images")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(runtime / "images"), str(outside)],
        check=True, capture_output=True,
    )
    (result,) = read_images(runtime, [handle])
    assert result["isError"] is True
