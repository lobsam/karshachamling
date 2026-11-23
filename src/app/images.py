"""Turn an uploaded image into web-friendly WebP sizes and store them in R2."""

import io
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath

from app.storage import Storage

VARIANT_WIDTHS = {"sm": 400, "md": 1200, "lg": 2000}
ALLOWED_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}
EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}
# Workers have 128 MB of memory; refuse images that would decode larger than ~100 MB
MAX_PIXELS = 25_000_000


class InvalidImage(ValueError):
    pass


@dataclass
class StoredImage:
    original_key: str
    variants: dict[str, str]
    mime_type: str
    width: int
    height: int
    size_bytes: int


def _safe_stem(filename: str) -> str:
    stem = PurePath(filename).stem.lower()
    cleaned = "".join(c if c.isalnum() else "-" for c in stem).strip("-")
    return "-".join(filter(None, cleaned.split("-")))[:60] or "image"


def _render_variants(data: bytes) -> tuple[str, int, int, list[tuple[int, bytes]]]:
    from PIL import Image, ImageOps, UnidentifiedImageError  # lazy import (Worker startup)

    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
    except (UnidentifiedImageError, OSError) as e:
        raise InvalidImage("File is not a supported image") from e
    if fmt not in ALLOWED_FORMATS:
        raise InvalidImage("Only JPEG, PNG, WebP and GIF images are allowed")

    orientation = img.getexif().get(0x0112, 1)
    w0, h0 = img.size
    width, height = (h0, w0) if orientation in (5, 6, 7, 8) else (w0, h0)
    if fmt != "JPEG" and w0 * h0 > MAX_PIXELS:
        raise InvalidImage("Image is too large; please resize it below 25 megapixels")
    if fmt == "JPEG":
        # Decode big JPEGs at reduced scale straight away — saves lots of memory and CPU
        img.draft("RGB", (VARIANT_WIDTHS["lg"], VARIANT_WIDTHS["lg"]))
    try:
        img.load()
    except (OSError, Image.DecompressionBombError) as e:
        raise InvalidImage("Image could not be read") from e

    img = ImageOps.exif_transpose(img)  # respect phone camera rotation
    if img.mode not in ("RGB", "RGBA"):
        has_alpha = "transparency" in img.info or img.mode in ("LA", "P", "PA")
        img = img.convert("RGBA" if has_alpha else "RGB")

    outputs: list[tuple[int, bytes]] = []
    for target in sorted(set(min(w, width) for w in VARIANT_WIDTHS.values()), reverse=True):
        if target < img.width:
            img = img.resize(
                (target, max(1, round(img.height * target / img.width))), Image.LANCZOS
            )
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=82, method=4)
        outputs.append((target, buf.getvalue()))
    return fmt, width, height, outputs


async def process_and_store(data: bytes, filename: str, storage: Storage) -> StoredImage:
    fmt, width, height, outputs = _render_variants(data)
    base = f"{datetime.now(UTC):%Y/%m}/{_safe_stem(filename)}-{secrets.token_hex(4)}"
    original_key = f"{base}/original.{EXTENSIONS[fmt]}"
    await storage.put(original_key, data, ALLOWED_FORMATS[fmt])
    keys: dict[int, str] = {}
    for w, webp in outputs:
        keys[w] = f"{base}/{w}.webp"
        await storage.put(keys[w], webp, "image/webp")
    variants = {name: keys[min(w, width)] for name, w in VARIANT_WIDTHS.items()}
    return StoredImage(original_key, variants, ALLOWED_FORMATS[fmt], width, height, len(data))
