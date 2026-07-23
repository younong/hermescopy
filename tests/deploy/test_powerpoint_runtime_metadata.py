from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "deploy/powerpoint-runtime"


def test_pptxgenjs_runtime_is_exact_locked_and_script_free():
    package = json.loads((RUNTIME / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((RUNTIME / "package-lock.json").read_text(encoding="utf-8"))

    assert package["private"] is True
    assert package["dependencies"] == {"pptxgenjs": "4.0.1"}
    assert lock["packages"]["node_modules/pptxgenjs"]["version"] == "4.0.1"
    assert all("hasInstallScript" not in item for item in lock["packages"].values())
    assert not any(path.endswith(".node") for path in lock["packages"])


def test_alicloud_powerpoint_manifest_is_closed_and_exact():
    document = json.loads(
        (ROOT / "deploy/runtime/alicloud3-powerpoint-packages.json").read_text(
            encoding="utf-8"
        )
    )

    assert document["schemaVersion"] == 1
    assert document["distribution"] == {
        "id": "alinux",
        "versionId": "3",
        "platformId": "platform:al8",
        "architecture": "x86_64",
    }
    assert {item["name"] for item in document["packages"]} >= {
        "dejavu-fonts-common",
        "fontconfig",
        "liberation-fonts-common",
        "liberation-narrow-fonts",
        "libreoffice-core",
        "libreoffice-help-en",
        "libreoffice-impress",
        "libreoffice-langpack-en",
        "libreoffice-opensymbol-fonts",
        "libreoffice-ure",
        "libreoffice-x11",
    }
    assert all(item["nevra"].startswith(item["name"] + "-") for item in document["packages"])
