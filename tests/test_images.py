import copy
import json
import os

import pytest

from claude_native_bridge import images
from claude_native_bridge.images import (
    ImageError,
    decode_image_part,
    externalize_images,
    purge_images,
)
from claude_native_bridge.protocol import HistoryTracker, ImageNotSeen, ProtocolError

from image_fixtures import GIF, JPEG, PNG, WEBP, data_uri, image_part


def user(*parts):
    return {"role": "user", "content": list(parts)}


@pytest.mark.parametrize(
    "data,mime",
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
def test_supported_formats_decode_exactly(data, mime):
    image = decode_image_part(image_part(data, mime, detail="high"))
    assert (image.mime, image.data) == (mime, data)


def test_jpg_alias_normalizes_and_string_source_is_accepted():
    image = decode_image_part(
        {"type": "input_image", "image_url": data_uri(JPEG, "image/jpg")}
    )
    assert image.mime == "image/jpeg"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/a.png",
        "http://127.0.0.1/a.png",
        "file:///etc/passwd",
        "/etc/passwd",
        "../secret.png",
        "ftp://example.invalid/a.png",
    ],
)
def test_non_data_sources_are_rejected_without_being_opened(url, tmp_path):
    target = tmp_path / "secret.png"
    target.write_bytes(PNG)
    for source in (url, str(target), target.as_uri()):
        with pytest.raises(ImageError) as caught:
            decode_image_part({"type": "image_url", "image_url": {"url": source}})
        assert "not seen" in str(caught.value)


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": "data:image/svg+xml;base64,PHN2Zz4="}},
        {"type": "image_url", "image_url": {"url": "data:image/png;charset=x;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "data:image/png,rawbytes"}},
        {"type": "image_url", "image_url": {"url": data_uri(PNG)[:-1]}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,@@@@"}},
        {"type": "image_url", "image_url": {"url": data_uri(b"")}},
        {"type": "image_url", "image_url": {"url": data_uri(JPEG)}},
        {"type": "image_url", "image_url": {"url": data_uri(b"not an image")}},
        {"type": "image_url", "image_url": {"url": data_uri(PNG), "path": "/x"}},
        {"type": "image_url", "image_url": {}},
        {"type": "image_url", "image_url": 7},
        {"type": "image_url", "image_url": {"url": data_uri(PNG)}, "extra": 1},
    ],
)
def test_malformed_or_unsupported_images_say_not_seen(part):
    with pytest.raises(ImageError, match="not seen"):
        decode_image_part(part)
    with pytest.raises(ImageNotSeen, match="not seen"):
        HistoryTracker().prepare([user(part)], None)


def test_oversize_image_is_rejected(monkeypatch):
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 16)
    with pytest.raises(ImageError, match="per-image limit"):
        decode_image_part(image_part(PNG + b"x" * 64))
    decode_image_part(image_part(PNG))


def test_roles_and_message_level_image_fields():
    for role in ("assistant", "system", "developer"):
        message = {"role": role, "content": [image_part()]}
        with pytest.raises(ImageNotSeen, match="user and tool"):
            HistoryTracker().prepare([message], None)
    tool = {"role": "tool", "tool_call_id": "c", "content": [image_part()]}
    HistoryTracker().prepare([user({"type": "text", "text": "x"}), tool], None)
    with pytest.raises(ProtocolError):
        HistoryTracker().prepare([{"role": "user", "content": "x", "images": [1]}], None)


def test_externalize_replaces_in_place_keeps_order_and_canonical_untouched(tmp_path):
    messages = [
        {"role": "system", "content": "rules"},
        user(
            {"type": "text", "text": "before"},
            image_part(PNG),
            {"type": "text", "text": "between"},
            image_part(JPEG, "image/jpeg"),
            image_part(PNG),
        ),
        {"role": "tool", "tool_call_id": "c", "content": [image_part(GIF, "image/gif")]},
    ]
    before = copy.deepcopy(messages)

    native = externalize_images(messages, tmp_path)

    assert messages == before
    parts = native[1]["content"]
    assert [p["type"] for p in parts] == ["text"] * 5
    assert parts[0]["text"] == "before" and parts[2]["text"] == "between"
    assert parts[1]["text"] == parts[4]["text"] != parts[3]["text"]
    assert "base64" not in json.dumps(native)
    assert "mcp__hermesbridge__read_image" in parts[1]["text"]
    assert "not been shown" in parts[1]["text"]
    stored = {p.name: p.read_bytes() for p in (tmp_path / "images").iterdir()}
    assert sorted(stored.values()) == sorted([PNG, JPEG, GIF])
    assert all(name.startswith("i") for name in stored)
    for handle_text in (parts[1]["text"], parts[3]["text"], native[2]["content"][0]["text"]):
        handle = handle_text.split('handle="')[1].split('"')[0]
        assert any(name.startswith(handle) for name in stored)
    if os.name != "nt":
        assert (tmp_path / "images").stat().st_mode & 0o777 == 0o700
        assert {p.stat().st_mode & 0o777 for p in (tmp_path / "images").iterdir()} == {0o600}
    HistoryTracker().prepare(native, None)


def test_handles_are_stable_across_requests_and_sessions(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    first = externalize_images([user(image_part())], other)
    second = externalize_images([user(image_part())], tmp_path)
    again = externalize_images([user(image_part())], tmp_path)
    assert second == again == first


def test_store_is_synced_to_the_current_request_only(tmp_path):
    externalize_images([user(image_part(PNG), image_part(JPEG, "image/jpeg"))], tmp_path)
    assert len(list((tmp_path / "images").iterdir())) == 2
    (tmp_path / "images" / "stray.txt").write_text("stale")
    externalize_images([user(image_part(JPEG, "image/jpeg"))], tmp_path)
    assert [p.read_bytes() for p in (tmp_path / "images").iterdir()] == [JPEG]
    externalize_images([user({"type": "text", "text": "no images"})], tmp_path)
    assert not (tmp_path / "images").exists()


def test_text_only_requests_create_no_store_and_return_equal_messages(tmp_path):
    messages = [{"role": "user", "content": "hi"}, user({"type": "text", "text": "x"})]
    assert externalize_images(messages, tmp_path) == messages
    assert not (tmp_path / "images").exists()
    assert externalize_images(messages, None) == messages


def test_count_total_and_missing_runtime_are_explicit_errors(tmp_path, monkeypatch):
    with pytest.raises(ImageError, match="runtime unavailable"):
        externalize_images([user(image_part())], None)
    monkeypatch.setattr(images, "MAX_IMAGES", 1)
    with pytest.raises(ImageError, match="at most 1 images"):
        externalize_images([user(image_part(PNG), image_part(JPEG, "image/jpeg"))], tmp_path)
    monkeypatch.setattr(images, "MAX_IMAGES", 64)
    monkeypatch.setattr(images, "MAX_TOTAL_IMAGE_BYTES", len(PNG) + 3)
    with pytest.raises(ImageError, match="total limit"):
        externalize_images([user(image_part(PNG), image_part(JPEG, "image/jpeg"))], tmp_path)


def test_store_write_failure_names_unseen_image(tmp_path):
    (tmp_path / "images").write_text("a file, not a directory")
    with pytest.raises(ImageError, match="not seen"):
        externalize_images([user(image_part())], tmp_path)


def test_purge_removes_blobs_and_a_symlinked_store_without_following_it(tmp_path):
    externalize_images([user(image_part())], tmp_path)
    purge_images(tmp_path)
    assert not (tmp_path / "images").exists()
    purge_images(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.png").write_bytes(PNG)
    try:
        (tmp_path / "images").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    purge_images(tmp_path)
    assert (outside / "keep.png").exists() and not (tmp_path / "images").exists()


def _only_blob(tmp_path):
    (blob,) = (tmp_path / "images").iterdir()
    return blob


def test_same_size_tampered_blob_is_rewritten_not_reused(tmp_path):
    externalize_images([user(image_part(PNG))], tmp_path)
    blob = _only_blob(tmp_path)
    blob.write_bytes(b"\x89PNG\r\n\x1a\n" + b"EVIL-bod")  # same length as PNG
    assert len(blob.read_bytes()) == len(PNG) and blob.read_bytes() != PNG
    externalize_images([user(image_part(PNG))], tmp_path)
    assert blob.read_bytes() == PNG


def test_unchanged_blob_is_reused_without_rewrite(tmp_path):
    externalize_images([user(image_part(PNG))], tmp_path)
    blob = _only_blob(tmp_path)
    before = (blob.stat().st_ino, blob.stat().st_mtime_ns)
    externalize_images([user(image_part(PNG))], tmp_path)
    assert (blob.stat().st_ino, blob.stat().st_mtime_ns) == before
    assert [p.name for p in (tmp_path / "images").iterdir()] == [blob.name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_reused_blob_with_widened_mode_or_extra_link_is_replaced(tmp_path):
    externalize_images([user(image_part(PNG))], tmp_path)
    blob = _only_blob(tmp_path)
    blob.chmod(0o644)
    externalize_images([user(image_part(PNG))], tmp_path)
    assert blob.stat().st_mode & 0o777 == 0o600
    alias = tmp_path / "alias"
    os.link(blob, alias)
    externalize_images([user(image_part(PNG))], tmp_path)
    assert blob.stat().st_nlink == 1 and alias.read_bytes() == PNG
