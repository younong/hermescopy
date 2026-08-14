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
class ImageCustomSizeConstraints:
    step: int
    min_pixels: int
    max_pixels: int
    max_side: int
    max_aspect_ratio: float


@dataclass(frozen=True)
class ImageSizeProfile:
    name: str
    native_sizes: tuple[ImageNativeSize, ...]
    custom_constraints: ImageCustomSizeConstraints | None = None


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


GPT_IMAGE_2_SIZE_PROFILE = ImageSizeProfile(
    name="gpt-image-2",
    custom_constraints=ImageCustomSizeConstraints(
        step=16,
        min_pixels=655_360,
        max_pixels=8_294_400,
        max_side=3840,
        max_aspect_ratio=3.0,
    ),
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
    GPT_IMAGE_2_SIZE_PROFILE.name: GPT_IMAGE_2_SIZE_PROFILE,
}
_RESOLUTION_ORDER = {"1K": 1, "2K": 2, "4K": 4}
_FORMAT_MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def image_size_profile(name: str) -> ImageSizeProfile:
    try:
        return _IMAGE_SIZE_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown image size profile: {name!r}") from exc


def _custom_size_for_aspect(
    aspect_ratio: str,
    *,
    target_long_side: int,
    constraints: ImageCustomSizeConstraints,
) -> tuple[str, int, int]:
    requested_value = aspect_ratio_value(aspect_ratio)
    target_value = min(
        max(requested_value, 1 / constraints.max_aspect_ratio),
        constraints.max_aspect_ratio,
    )
    effective_aspect = (
        aspect_ratio
        if target_value == requested_value
        else aspect_ratio_from_dimensions(
            int(constraints.max_aspect_ratio),
            1,
        ) if requested_value > 1 else aspect_ratio_from_dimensions(
            1,
            int(constraints.max_aspect_ratio),
        )
    )
    landscape = target_value >= 1
    long_to_short = target_value if landscape else 1 / target_value
    min_long_side = max(
        constraints.step,
        int((constraints.min_pixels * long_to_short) ** 0.5),
    )
    max_long_side = min(
        constraints.max_side,
        int((constraints.max_pixels * long_to_short) ** 0.5),
    )
    min_long_side = max(
        constraints.step,
        (min_long_side // constraints.step - 1) * constraints.step,
    )
    max_long_side = min(
        constraints.max_side,
        (max_long_side // constraints.step + 1) * constraints.step,
    )
    best: tuple[float, int, int] | None = None
    for long_side in range(min_long_side, max_long_side + 1, constraints.step):
        rounded_short = round(
            long_side / long_to_short / constraints.step
        ) * constraints.step
        for short_side in range(
            rounded_short - constraints.step,
            rounded_short + constraints.step * 2,
            constraints.step,
        ):
            if short_side <= 0 or short_side > long_side:
                continue
            pixels = long_side * short_side
            if not constraints.min_pixels <= pixels <= constraints.max_pixels:
                continue
            actual_value = long_side / short_side
            if actual_value > constraints.max_aspect_ratio:
                continue
            score = (
                abs(long_side - target_long_side) / target_long_side
                + 2 * abs(actual_value - long_to_short) / long_to_short
            )
            width, height = (
                (long_side, short_side) if landscape else (short_side, long_side)
            )
            candidate = (score, width, height)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        raise ValueError(f"No valid image size for aspect ratio {aspect_ratio}")
    return effective_aspect, best[1], best[2]


def resolve_image_size(
    aspect_ratio: str | None,
    resolution: str | None,
    *,
    profile: ImageSizeProfile,
) -> ImageSizePlan:
    """Resolve a unified request to a provider-valid output size."""
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
    native = next(
        (item for item in candidates if item.aspect_ratio == requested_aspect),
        None,
    )
    if native is not None:
        effective_aspect = requested_aspect
        width, height = native.width, native.height
    elif profile.custom_constraints is not None:
        nearest = nearest_aspect_ratio(
            requested_aspect,
            tuple(item.aspect_ratio for item in candidates),
        )
        target = next(item for item in candidates if item.aspect_ratio == nearest)
        effective_aspect, width, height = _custom_size_for_aspect(
            requested_aspect,
            target_long_side=max(target.width, target.height),
            constraints=profile.custom_constraints,
        )
    else:
        effective_aspect = nearest_aspect_ratio(
            requested_aspect,
            tuple(item.aspect_ratio for item in candidates),
        )
        native = next(
            item for item in candidates if item.aspect_ratio == effective_aspect
        )
        width, height = native.width, native.height
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
        width=width,
        height=height,
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
    require_exact_dimensions: bool = True,
    aspect_tolerance: float = 0.01,
) -> ActualImageInfo:
    """Verify decoded dimensions against fixed-size or ratio-only expectations."""
    plan_dimensions = None
    if plan is not None:
        plan_dimensions = (plan.width, plan.height)
        effective_aspect_ratio = plan.effective_aspect_ratio
        if require_exact_dimensions:
            expected_dimensions = plan_dimensions
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
            if plan is not None
            and (actual.width, actual.height) == plan_dimensions
            else None
        ),
    )


def image_prompt_with_size_requirements(
    prompt: str,
    plan: ImageSizePlan,
) -> str:
    """Append one unambiguous ratio contract plus best-effort size hints."""
    return (
        f"{prompt.rstrip()}\n\n"
        "Output requirements:\n"
        f"- Aspect ratio: exactly {plan.effective_aspect_ratio} (width:height).\n"
        f"- Resolution tier: {plan.effective_resolution}.\n"
        f"- Preferred pixel dimensions: {plan.size} pixels.\n"
        f"- Keep the final canvas at exactly {plan.effective_aspect_ratio}; "
        "do not crop or change the requested aspect ratio."
    )
