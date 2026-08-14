import io

import pytest
from PIL import Image

from agent.image_gen_provider import VALID_RESOLUTIONS, canonical_aspect_ratio
from agent.image_size import (
    GPT_IMAGE_2_SIZE_PROFILE,
    image_prompt_with_size_requirements,
    inspect_image_bytes,
    resolve_image_size,
    validate_image_output,
)


def _image_bytes(size=(16, 12), image_format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", size).save(output, format=image_format)
    return output.getvalue()


def test_gpt_image_2_profile_preserves_requested_ratio_and_resolution():
    plan = resolve_image_size(
        "3:4", "2K", profile=GPT_IMAGE_2_SIZE_PROFILE
    )

    assert plan.requested_aspect_ratio == "3:4"
    assert plan.effective_aspect_ratio == "3:4"
    assert plan.requested_resolution == "2K"
    assert plan.effective_resolution == "2K"
    assert plan.resolution_mode == "native"
    assert plan.size == "1536x2048"


def test_gpt_image_2_profile_covers_native_ratio_resolution_pairs():
    native_pairs = {
        (size.aspect_ratio, size.resolution)
        for size in GPT_IMAGE_2_SIZE_PROFILE.native_sizes
    }

    native_aspects = {size.aspect_ratio for size in GPT_IMAGE_2_SIZE_PROFILE.native_sizes}
    assert native_pairs == {
        (aspect_ratio, resolution)
        for aspect_ratio in native_aspects
        for resolution in VALID_RESOLUTIONS
    }
    for aspect_ratio, resolution in native_pairs:
        plan = resolve_image_size(
            aspect_ratio, resolution, profile=GPT_IMAGE_2_SIZE_PROFILE
        )
        assert plan.effective_aspect_ratio == aspect_ratio
        assert plan.effective_resolution == resolution
        assert plan.resolution_mode == "native"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2.35:1", "47:20"),
        (" 21 : 9 ", "7:3"),
        ("4:4", "1:1"),
        ("portrait", "3:4"),
    ],
)
def test_canonical_aspect_ratio_accepts_arbitrary_positive_ratios(value, expected):
    assert canonical_aspect_ratio(value) == expected


@pytest.mark.parametrize("value", ["", "wide", "0:1", "1:0", "-1:2", "1/2"])
def test_canonical_aspect_ratio_rejects_invalid_values(value):
    assert canonical_aspect_ratio(value) is None


def test_gpt_image_2_profile_computes_provider_valid_custom_size():
    plan = resolve_image_size(
        "2.35:1", "4K", profile=GPT_IMAGE_2_SIZE_PROFILE
    )

    assert plan.requested_aspect_ratio == "47:20"
    assert plan.effective_resolution == "4K"
    assert plan.width % 16 == plan.height % 16 == 0
    assert max(plan.width, plan.height) <= 3840
    assert 655_360 <= plan.width * plan.height <= 8_294_400
    assert max(plan.width / plan.height, plan.height / plan.width) <= 3
    assert abs(plan.width / plan.height - 2.35) < 0.01


def test_gpt_image_2_profile_maps_extreme_ratio_to_api_limit():
    plan = resolve_image_size(
        "4:1", "2K", profile=GPT_IMAGE_2_SIZE_PROFILE
    )

    assert plan.requested_aspect_ratio == "4:1"
    assert plan.effective_aspect_ratio == "3:1"
    assert plan.resolution_mode == "mapped"


def test_gpt_image_2_profile_preserves_exact_native_tier_and_legacy_alias():
    plan = resolve_image_size(
        "portrait", "4k", profile=GPT_IMAGE_2_SIZE_PROFILE
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
        "1:1", "1K", profile=GPT_IMAGE_2_SIZE_PROFILE
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


def test_validate_image_output_accepts_non_native_dimensions_when_ratio_is_exact():
    plan = resolve_image_size(
        "3:4", "2K", profile=GPT_IMAGE_2_SIZE_PROFILE
    )
    actual = inspect_image_bytes(_image_bytes((1086, 1448)))

    verified = validate_image_output(
        actual,
        plan=plan,
        require_exact_dimensions=False,
    )

    assert verified.actual_aspect_ratio == "3:4"
    assert verified.actual_resolution is None


def test_ratio_prompt_includes_exact_ratio_and_best_effort_size_hints():
    plan = resolve_image_size(
        "3:4", "2K", profile=GPT_IMAGE_2_SIZE_PROFILE
    )

    constrained = image_prompt_with_size_requirements("draw a cat", plan)

    assert constrained.startswith("draw a cat\n\nOutput requirements:")
    assert "Aspect ratio: exactly 3:4 (width:height)." in constrained
    assert "Resolution tier: 2K." in constrained
    assert "Preferred pixel dimensions: 1536x2048 pixels." in constrained
