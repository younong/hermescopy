import io

import pytest
from PIL import Image

from agent.image_size import (
    APIYI_GPT_IMAGE_PROFILE,
    OPENAI_NATIVE_IMAGE_PROFILE,
    inspect_image_bytes,
    resolve_image_size,
    validate_image_output,
)


def _image_bytes(size=(16, 12), image_format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", size).save(output, format=image_format)
    return output.getvalue()


def test_openai_native_profile_truthfully_maps_ratio_and_resolution():
    plan = resolve_image_size(
        "3:4", "2K", profile=OPENAI_NATIVE_IMAGE_PROFILE
    )

    assert plan.requested_aspect_ratio == "3:4"
    assert plan.effective_aspect_ratio == "2:3"
    assert plan.requested_resolution == "2K"
    assert plan.effective_resolution == "1K"
    assert plan.resolution_mode == "mapped"
    assert plan.size == "1024x1536"


def test_apiyi_profile_preserves_exact_native_tier_and_legacy_alias():
    plan = resolve_image_size(
        "portrait", "4k", profile=APIYI_GPT_IMAGE_PROFILE
    )

    assert plan.requested_aspect_ratio == "3:4"
    assert plan.effective_aspect_ratio == "3:4"
    assert plan.requested_resolution == "4K"
    assert plan.effective_resolution == "4K"
    assert plan.resolution_mode == "native"
    assert plan.size == "2480x3312"


@pytest.mark.parametrize(
    ("image_format", "mime_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_inspect_image_bytes_uses_decoded_format_and_dimensions(
    image_format, mime_type
):
    actual = inspect_image_bytes(
        _image_bytes((16, 12), image_format), declared_mime_type=mime_type
    )

    assert actual.width == 16
    assert actual.height == 12
    assert actual.actual_aspect_ratio == "4:3"
    assert actual.mime_type == mime_type


def test_inspect_image_bytes_rejects_invalid_mime_and_pixels():
    image = _image_bytes((16, 12))
    with pytest.raises(ValueError, match="MIME"):
        inspect_image_bytes(image, declared_mime_type="image/jpeg")
    with pytest.raises(ValueError, match="pixel limit"):
        inspect_image_bytes(image, max_pixels=100)
    with pytest.raises(ValueError, match="invalid"):
        inspect_image_bytes(b"not-an-image")


def test_validate_image_output_requires_exact_fixed_size():
    plan = resolve_image_size(
        "1:1", "1K", profile=OPENAI_NATIVE_IMAGE_PROFILE
    )
    actual = inspect_image_bytes(_image_bytes((512, 512)))

    with pytest.raises(ValueError, match="512x512.*1024x1024"):
        validate_image_output(actual, plan=plan)


def test_validate_image_output_supports_ratio_only_contract():
    actual = inspect_image_bytes(_image_bytes((300, 400)))

    verified = validate_image_output(
        actual, effective_aspect_ratio="3:4"
    )
    assert verified.actual_aspect_ratio == "3:4"
    assert verified.actual_resolution is None

    with pytest.raises(ValueError, match="aspect ratio"):
        validate_image_output(actual, effective_aspect_ratio="1:1")
