from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "deploy" / "smoke-apiyi.py"


@pytest.fixture
def smoke_module():
    spec = importlib.util.spec_from_file_location("_smoke_apiyi", SMOKE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.mark.parametrize(
    ("resolution", "size"),
    (("1K", "768x1024"), ("2K", "1536x2048"), ("4K", "2480x3312")),
)
def test_gpt_3_4_smoke_validates_resolution_size(smoke_module, resolution, size):
    class Provider:
        def generate(self, prompt, **kwargs):
            assert prompt == "draw"
            assert kwargs == {
                "aspect_ratio": "3:4",
                "resolution": resolution,
                "model": "gpt-image-2-medium",
            }
            return {
                "success": True,
                "provider": "apiyi",
                "image": "/tmp/result.png",
                "requested_aspect_ratio": "3:4",
                "effective_aspect_ratio": "3:4",
                "requested_resolution": resolution,
                "effective_resolution": resolution,
                "quality": "medium",
                "size": size,
            }

    result = smoke_module._run_model(
        Provider(), "gpt-image-2-medium", "draw", "3:4", resolution
    )

    assert result["success"] is True
    assert result["size"] == size


def test_smoke_rejects_stale_gpt_3_4_size(smoke_module):
    class Provider:
        def generate(self, prompt, **kwargs):
            return {
                "success": True,
                "requested_aspect_ratio": "3:4",
                "effective_aspect_ratio": "3:4",
                "requested_resolution": "2K",
                "effective_resolution": "2K",
                "size": "768x1024",
            }

    result = smoke_module._run_model(
        Provider(), "gpt-image-2-medium", "draw", "3:4", "2K"
    )

    assert result["success"] is False
    assert result["error_type"] == "image_contract_validation"
