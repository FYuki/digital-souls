from __future__ import annotations

from io import BytesIO
import warnings

from PIL import Image, UnidentifiedImageError

from app.inference.contracts import (
    ImageInputLimits,
    InferenceImagePart,
    InferenceMessage,
)
from app.inference.errors import InferenceError, InferenceErrorCategory


ALLOWED_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg"})
MAX_IMAGE_BYTES = 5_242_880
MAX_IMAGE_WIDTH = 2_560
MAX_IMAGE_HEIGHT = 2_560
MAX_IMAGE_PIXELS = 4_194_304
MAX_IMAGES_PER_REQUEST = 1
CONSERVATIVE_IMAGE_TOKEN_ESTIMATE = 1_120
SCREEN_IMAGE_INPUT_LIMITS = ImageInputLimits(
    allowed_mime_types=ALLOWED_IMAGE_MIME_TYPES,
    max_images=MAX_IMAGES_PER_REQUEST,
    max_bytes=MAX_IMAGE_BYTES,
    max_width=MAX_IMAGE_WIDTH,
    max_height=MAX_IMAGE_HEIGHT,
    max_pixels=MAX_IMAGE_PIXELS,
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_FORMATS_BY_MIME_TYPE = {"image/png": "PNG", "image/jpeg": "JPEG"}


def image_parts(messages: tuple[InferenceMessage, ...]) -> tuple[InferenceImagePart, ...]:
    return tuple(
        part
        for message in messages
        if isinstance(message.content, tuple)
        for part in message.content
        if isinstance(part, InferenceImagePart)
    )


def validate_multimodal_messages(
    messages: tuple[InferenceMessage, ...],
    limits: ImageInputLimits,
) -> int:
    """画像本文を外へ出さず、MIME、magic、decode、寸法、量を検証する。"""
    images = image_parts(messages)
    if len(images) > limits.max_images:
        _invalid_image()
    for image in images:
        _validate_image(image, limits)
    return len(images)


def _validate_image(image: InferenceImagePart, limits: ImageInputLimits) -> None:
    if image.mime_type not in limits.allowed_mime_types:
        _invalid_image()
    if not image.data or len(image.data) > limits.max_bytes:
        _invalid_image()
    expected_format = _FORMATS_BY_MIME_TYPE[image.mime_type]
    if expected_format == "PNG" and not image.data.startswith(_PNG_SIGNATURE):
        _invalid_image()
    if expected_format == "JPEG" and not image.data.startswith(_JPEG_SIGNATURE):
        _invalid_image()
    if image.width > limits.max_width or image.height > limits.max_height:
        _invalid_image()
    if image.width * image.height > limits.max_pixels:
        _invalid_image()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image.data)) as decoded:
                if (
                    decoded.format != expected_format
                    or decoded.size != (image.width, image.height)
                    or getattr(decoded, "n_frames", 1) != 1
                ):
                    _invalid_image()
                decoded.verify()
            with Image.open(BytesIO(image.data)) as decoded:
                decoded.load()
    except InferenceError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ):
        _invalid_image()


def _invalid_image() -> None:
    raise InferenceError(
        InferenceErrorCategory.INVALID_REQUEST,
        retryable=False,
        message="inference image is invalid",
    )
