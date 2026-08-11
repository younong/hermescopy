"""Shared native-size planning and generated-image verification."""
from __future__ import annotations

import io
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Literal

from agent.image_gen_provider import (
    aspect_ratio_from_dimensions,
    aspect_ratio_value,
    nearest_aspect_ratio,
    resolve_aspect_ratio,
    resolve_resolution,
)

ResolutionMode = Literal["native", "mapped"]


@dataclass(frozen=True)
class ImageNativeSize:
    aspect_ratio: str
    resolution: str
    width: int
    height: int

    @property
    def size(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass(frozen=True)
class ImageSizeProfile:
    name: str
    native_sizes: tuple[ImageNativeSize, ...]


@dataclass(frozen=True)
class ImageSizePlan:
    requested_aspect_ratio: str
    effective_aspect_ratio: str
    requested_resolution: str
    effective_resolution: str
    resolution_mode: ResolutionMode
    width: int
    height: int

    @property
    def size(self) -> str:
        return f"{self.width}x{self.height}"

    def metadata(self) -> dict[str, object]:
        return {
            "size": self.size,
            "requested_aspect_ratio": self.requested_aspect_ratio,
            "effective_aspect_ratio": self.effective_aspect_ratio,
            "requested_resolution": self.requested_resolution,
            "effective_resolution": self.effective_resolution,
            "resolution_mode": self.resolution_mode,
        }


@dataclass(frozen=True)
class ActualImageInfo:
    width: int
    height: int
    actual_aspect_ratio: str
    mime_type: str
    format: str
    actual_resolution: str | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "actual_dimensions": {"width": self.width, "height": self.height},
            "actual_aspect_ratio": self.actual_aspect_ratio,
            "actual_resolution": self.actual_resolution,
        }


OPENAI_NATIVE_IMAGE_PROFILE = ImageSizeProfile(
    name="openai-native",
    native_sizes=(
        ImageNativeSize("3:2", "1K", 1536, 1024),
        ImageNativeSize("1:1", "1K", 1024, 1024),
        ImageNativeSize("2:3", "1K", 1024, 1536),
    ),
)

APIYI_GPT_IMAGE_PROFILE = ImageSizeProfile(
    name="apiyi-gpt",
    native_sizes=tuple(
        ImageNativeSize(aspect_ratio, resolution, width, height)
        for resolution, sizes in (
            ("1K", (
                ("16:9", 1280, 720), ("3:2", 1536, 1024),
                ("4:3", 1024, 768), ("1:1", 1024, 1024),
                ("3:4", 768, 1024), ("2:3", 1024, 1536),
                ("9:16", 720, 1280),
            )),
            ("2K", (
                ("16:9", 2048, 1152), ("3:2", 2048, 1360),
                ("4:3", 2048, 1536), ("1:1", 2048, 2048),
                ("3:4", 1536, 2048), ("2:3", 1360, 2048),
                ("9:16", 1152, 2048),
            )),
            ("4K", (
                ("16:9", 3840, 2160), ("3:2", 3520, 2336),
                ("4:3", 3312, 2480), ("1:1", 2880, 2880),
                ("3:4", 2480, 3312), ("2:3", 2336, 3520),
                ("9:16", 2160, 3840),
            )),
        )
        for aspect_ratio, width, height in sizes
    ),
)

_IMAGE_SIZE_PROFILES = {
    profile.name: profile
    for profile in (OPENAI_NATIVE_IMAGE_PROFILE, APIYI_GPT_IMAGE_PROFILE)
}
_RESOLUTION_ORDER = {"1K": 1, "2K": 2, "4K": 4}
_FORMAT_MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def image_size_profile(name: str) -> ImageSizeProfile:
    try:
        return _IMAGE_SIZE_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown image size profile: {name!r}") from exc


def resolve_image_size(
    aspect_ratio: str | None,
    resolution: str | None,
    *,
    profile: ImageSizeProfile,
) -> ImageSizePlan:
    """Resolve a unified request to one provider-native fixed output size."""
    requested_aspect = resolve_aspect_ratio(aspect_ratio)
    requested_resolution = resolve_resolution(resolution)
    resolutions = tuple(dict.fromkeys(item.resolution for item in profile.native_sizes))
    effective_resolution = min(
        resolutions,
        key=lambda item: abs(
            _RESOLUTION_ORDER[item] - _RESOLUTION_ORDER[requested_resolution]
        ),
    )
    candidates = tuple(
        item for item in profile.native_sizes
        if item.resolution == effective_resolution
    )
    effective_aspect = nearest_aspect_ratio(
        requested_aspect,
        tuple(item.aspect_ratio for item in candidates),
    )
    native = next(
        item for item in candidates if item.aspect_ratio == effective_aspect
    )
    mode: ResolutionMode = (
        "native"
        if effective_aspect == requested_aspect
        and effective_resolution == requested_resolution
        else "mapped"
    )
    return ImageSizePlan(
        requested_aspect_ratio=requested_aspect,
        effective_aspect_ratio=effective_aspect,
        requested_resolution=requested_resolution,
        effective_resolution=effective_resolution,
        resolution_mode=mode,
        width=native.width,
        height=native.height,
    )


class ImageAspectRatioMismatch(ValueError):
    """Decoded image dimensions do not match the effective aspect ratio."""


def _inspect_image(
    source: BinaryIO,
    *,
    declared_mime_type: str | None,
    max_pixels: int,
) -> ActualImageInfo:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(source) as image:
            image_format = str(image.format or "").upper()
            width, height = (int(value) for value in image.size)
            if image_format not in _FORMAT_MIME_TYPES or width <= 0 or height <= 0:
                raise ValueError("Generated image format is unsupported")
            if width * height > max_pixels:
                raise ValueError("Generated image exceeds the pixel limit")
            image.verify()
    except (OSError, SyntaxError, UnidentifiedImageError) as exc:
        raise ValueError("Generated image bytes are invalid") from exc

    mime_type = _FORMAT_MIME_TYPES[image_format]
    declared = str(declared_mime_type or "").strip().lower()
    if declared and declared != mime_type:
        raise ValueError("Generated image MIME type does not match its bytes")
    return ActualImageInfo(
        width=width,
        height=height,
        actual_aspect_ratio=aspect_ratio_from_dimensions(width, height),
        mime_type=mime_type,
        format=image_format,
    )


def inspect_image_bytes(
    image_bytes: bytes,
    *,
    declared_mime_type: str | None = None,
    max_output_bytes: int | None = None,
    max_pixels: int = 40_000_000,
) -> ActualImageInfo:
    """Decode safe raster metadata from bytes and reject mislabeled content."""
    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise ValueError("Generated image bytes are invalid")
    if max_output_bytes is not None and len(image_bytes) > max_output_bytes:
        raise ValueError("Generated image exceeds the output byte limit")
    return _inspect_image(
        io.BytesIO(image_bytes),
        declared_mime_type=declared_mime_type,
        max_pixels=max_pixels,
    )


def inspect_image_path(
    path: str | Path,
    *,
    declared_mime_type: str | None = None,
    max_output_bytes: int | None = None,
    max_pixels: int = 40_000_000,
) -> ActualImageInfo:
    """Decode safe raster metadata from a path without loading it all at once."""
    image_path = Path(path)
    if max_output_bytes is not None and image_path.stat().st_size > max_output_bytes:
        raise ValueError("Generated image exceeds the output byte limit")
    with image_path.open("rb") as source:
        return _inspect_image(
            source,
            declared_mime_type=declared_mime_type,
            max_pixels=max_pixels,
        )


def validate_image_output(
    actual: ActualImageInfo,
    *,
    plan: ImageSizePlan | None = None,
    effective_aspect_ratio: str | None = None,
    expected_dimensions: tuple[int, int] | None = None,
    aspect_tolerance: float = 0.01,
) -> ActualImageInfo:
    """Verify decoded dimensions against fixed-size or ratio-only expectations."""
    if plan is not None:
        expected_dimensions = (plan.width, plan.height)
        effective_aspect_ratio = plan.effective_aspect_ratio
    if expected_dimensions is not None and (
        actual.width, actual.height
    ) != expected_dimensions:
        raise ValueError(
            f"Generated image dimensions {actual.width}x{actual.height} do not match "
            f"the effective size {expected_dimensions[0]}x{expected_dimensions[1]}"
        )
    if effective_aspect_ratio and abs(
        aspect_ratio_value(actual.actual_aspect_ratio)
        - aspect_ratio_value(resolve_aspect_ratio(effective_aspect_ratio))
    ) > aspect_tolerance:
        raise ImageAspectRatioMismatch(
            f"Generated image aspect ratio {actual.actual_aspect_ratio} does not match "
            f"the effective ratio {resolve_aspect_ratio(effective_aspect_ratio)}"
        )
    return replace(
        actual,
        actual_resolution=(
            plan.effective_resolution
            if plan is not None and expected_dimensions is not None
            else None
        ),
    )


def parse_image_size(value: object) -> tuple[int, int] | None:
    """Parse a literal ``WIDTHxHEIGHT`` size used by fixed-size providers."""
    if not isinstance(value, str):
        return None
    width, separator, height = value.lower().partition("x")
    if not separator or not width.isdigit() or not height.isdigit():
        return None
    parsed = (int(width), int(height))
    return parsed if parsed[0] > 0 and parsed[1] > 0 else None
