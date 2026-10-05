import pytest

from flask_app.android_releases import normalize_android_version_name


def test_android_version_accepts_current_and_legacy_formats():
    assert normalize_android_version_name("v26.8.453") == "v26.8.453"
    assert normalize_android_version_name("26.1.452") == "26.1.452"


@pytest.mark.parametrize("version", ["v26.0.453", "v26.13.453", "26.8", "v2026.8.453"])
def test_android_version_rejects_invalid_formats(version):
    with pytest.raises(ValueError, match=r"vYY\.M\.increment"):
        normalize_android_version_name(version)
