"""Tiny valid-signature images and data-URI helpers shared by image tests."""

import base64

PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"
JPEG = b"\xff\xd8\xff" + b"jpeg-body"
GIF = b"GIF89a" + b"gif-body"
WEBP = b"RIFF" + bytes([1, 2, 3, 4]) + b"WEBP" + b"webp-body"


def data_uri(data, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def image_part(data=PNG, mime="image/png", **extra):
    return {"type": "image_url", "image_url": {"url": data_uri(data, mime), **extra}}
