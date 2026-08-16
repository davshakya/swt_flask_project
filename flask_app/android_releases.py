"""Android app release upload, metadata, and response helpers."""

from pathlib import Path
import hashlib
import io
import re
import secrets
import struct
import time
import zipfile


ANDROID_APK_CONTENT_TYPE = "application/vnd.android.package-archive"
ANDROID_BINARY_XML = 0x0003
ANDROID_BINARY_XML_STRING_POOL = 0x0001
ANDROID_BINARY_XML_RESOURCE_MAP = 0x0180
ANDROID_BINARY_XML_START_ELEMENT = 0x0102
ANDROID_TYPED_VALUE_STRING = 0x03
ANDROID_TYPED_VALUE_INT_DEC = 0x10
ANDROID_TYPED_VALUE_INT_HEX = 0x11
ANDROID_TYPED_VALUE_BOOLEAN = 0x12
ANDROID_STRING_POOL_UTF8_FLAG = 0x00000100
ANDROID_VERSION_CODE_RESOURCE_ID = 0x0101021B
ANDROID_VERSION_NAME_RESOURCE_ID = 0x0101021C
# New releases use vYY.M.increment. Keep the previous YY.train.increment form
# readable so existing uploaded APKs remain manageable during migration.
ANDROID_RELEASE_VERSION_PATTERN = re.compile(
    r"^(?:v\d{2}\.(?:[1-9]|1[0-2])\.[1-9]\d*|\d{2}\.[1-9]\d*\.[1-9]\d*)$"
)


def sanitize_android_apk_filename(filename):
    raw_name = Path(str(filename or "")).name.strip()
    if not raw_name:
        return "smart-water-tank.apk"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip(" .-_")
    if not safe_name:
        safe_name = "smart-water-tank"
    if not safe_name.lower().endswith(".apk"):
        safe_name = f"{safe_name}.apk"
    return safe_name


def android_release_storage_path(base_dir, stored_filename):
    return Path(base_dir) / Path(str(stored_filename or "")).name


def read_uploaded_android_apk(uploaded_file, max_bytes):
    if uploaded_file is None:
        raise ValueError("Choose an Android .apk file to upload.")

    raw_name = Path(str(uploaded_file.filename or "")).name.strip()
    if not raw_name:
        raise ValueError("Choose an Android .apk file to upload.")
    if Path(raw_name).suffix.lower() != ".apk":
        raise ValueError("Upload an Android .apk file.")
    safe_name = sanitize_android_apk_filename(raw_name)

    payload = uploaded_file.stream.read(max_bytes + 1)
    if not payload:
        raise ValueError("Uploaded Android app file was empty.")
    if len(payload) > max_bytes:
        raise ValueError(f"Android app upload is too large. Current limit is {max_bytes // (1024 * 1024)} MB.")
    if not payload.startswith(b"PK"):
        raise ValueError("Uploaded APK does not look like a valid Android package.")

    version_info = extract_android_apk_version(payload)

    return {
        "original_filename": safe_name,
        "payload": payload,
        "version_name": version_info["version_name"],
        "version_code": version_info["version_code"],
        "md5": hashlib.md5(payload).hexdigest(),
        "content_type": ANDROID_APK_CONTENT_TYPE,
    }


def make_stored_android_apk_filename(version_code):
    return f"smart-water-tank-{int(version_code)}-{int(time.time())}-{secrets.token_hex(4)}.apk"


def normalize_android_version_name(value):
    text = str(value or "").strip()
    if not text:
        raise ValueError("Android version name is required.")
    if not ANDROID_RELEASE_VERSION_PATTERN.match(text):
        raise ValueError("Android version name must use vYY.M.increment format, for example v26.8.453.")
    return text


def normalize_android_version_code(value):
    try:
        version_code = int(str(value or "").strip())
    except (TypeError, ValueError):
        raise ValueError("Android version code must be a number.") from None
    if version_code <= 0:
        raise ValueError("Android version code must be greater than zero.")
    return version_code


def _read_u16(data, offset):
    if offset + 2 > len(data):
        raise ValueError("Android manifest is truncated.")
    return struct.unpack_from("<H", data, offset)[0]


def _read_u32(data, offset):
    if offset + 4 > len(data):
        raise ValueError("Android manifest is truncated.")
    return struct.unpack_from("<I", data, offset)[0]


def _read_length8(data, offset):
    first = data[offset]
    if first & 0x80:
        return ((first & 0x7F) << 8) | data[offset + 1], offset + 2
    return first, offset + 1


def _read_length16(data, offset):
    first = _read_u16(data, offset)
    if first & 0x8000:
        return ((first & 0x7FFF) << 16) | _read_u16(data, offset + 2), offset + 4
    return first, offset + 2


def _decode_android_string_pool_string(data, offset, utf8):
    if utf8:
        _char_len, offset = _read_length8(data, offset)
        byte_len, offset = _read_length8(data, offset)
        raw = data[offset:offset + byte_len]
        return raw.decode("utf-8", errors="replace")

    char_len, offset = _read_length16(data, offset)
    raw = data[offset:offset + (char_len * 2)]
    return raw.decode("utf-16le", errors="replace")


def _parse_android_string_pool(data, chunk_offset, header_size, chunk_size):
    if header_size < 28:
        raise ValueError("Android manifest string pool is invalid.")
    string_count = _read_u32(data, chunk_offset + 8)
    flags = _read_u32(data, chunk_offset + 16)
    strings_start = _read_u32(data, chunk_offset + 20)
    utf8 = bool(flags & ANDROID_STRING_POOL_UTF8_FLAG)
    offsets_start = chunk_offset + header_size
    strings_base = chunk_offset + strings_start
    strings = []
    for index in range(string_count):
        string_offset = _read_u32(data, offsets_start + (index * 4))
        absolute_offset = strings_base + string_offset
        if absolute_offset >= chunk_offset + chunk_size:
            raise ValueError("Android manifest string pool offset is invalid.")
        strings.append(_decode_android_string_pool_string(data, absolute_offset, utf8))
    return strings


def _android_manifest_string(strings, index):
    if index == 0xFFFFFFFF:
        return None
    if index < 0 or index >= len(strings):
        return None
    return strings[index]


def _android_typed_value_to_text(strings, raw_value_index, data_type, data):
    raw_value = _android_manifest_string(strings, raw_value_index)
    if raw_value is not None:
        return raw_value
    if data_type == ANDROID_TYPED_VALUE_STRING:
        return _android_manifest_string(strings, data)
    if data_type in {ANDROID_TYPED_VALUE_INT_DEC, ANDROID_TYPED_VALUE_INT_HEX, ANDROID_TYPED_VALUE_BOOLEAN}:
        return str(data)
    return None


def _extract_android_versions_from_binary_manifest(manifest):
    strings = []
    resource_ids = []
    offset = 0
    manifest_len = len(manifest)

    while offset + 8 <= manifest_len:
        chunk_type = _read_u16(manifest, offset)
        header_size = _read_u16(manifest, offset + 2)
        chunk_size = _read_u32(manifest, offset + 4)
        if header_size < 8 or chunk_size < header_size or offset + chunk_size > manifest_len:
            raise ValueError("Android manifest contains an invalid binary XML chunk.")

        if chunk_type == ANDROID_BINARY_XML:
            offset += header_size
            continue
        if chunk_type == ANDROID_BINARY_XML_STRING_POOL:
            strings = _parse_android_string_pool(manifest, offset, header_size, chunk_size)
        elif chunk_type == ANDROID_BINARY_XML_RESOURCE_MAP:
            resource_ids = [
                _read_u32(manifest, resource_offset)
                for resource_offset in range(offset + header_size, offset + chunk_size, 4)
                if resource_offset + 4 <= offset + chunk_size
            ]
        elif chunk_type == ANDROID_BINARY_XML_START_ELEMENT:
            name_index = _read_u32(manifest, offset + 20)
            element_name = _android_manifest_string(strings, name_index)
            if element_name != "manifest":
                offset += chunk_size
                continue

            attribute_start = _read_u16(manifest, offset + 24)
            attribute_size = _read_u16(manifest, offset + 26)
            attribute_count = _read_u16(manifest, offset + 28)
            version_name = None
            version_code = None
            for index in range(attribute_count):
                attr_offset = offset + 16 + attribute_start + (index * attribute_size)
                if attr_offset + 20 > offset + chunk_size:
                    raise ValueError("Android manifest attribute data is invalid.")
                attr_name_index = _read_u32(manifest, attr_offset + 4)
                raw_value_index = _read_u32(manifest, attr_offset + 8)
                data_type = manifest[attr_offset + 15]
                data = _read_u32(manifest, attr_offset + 16)
                attr_name = _android_manifest_string(strings, attr_name_index)
                attr_resource_id = resource_ids[attr_name_index] if attr_name_index < len(resource_ids) else None
                attr_value = _android_typed_value_to_text(strings, raw_value_index, data_type, data)

                if attr_resource_id == ANDROID_VERSION_NAME_RESOURCE_ID or attr_name == "versionName":
                    version_name = attr_value
                elif attr_resource_id == ANDROID_VERSION_CODE_RESOURCE_ID or attr_name == "versionCode":
                    version_code = data if data_type in {ANDROID_TYPED_VALUE_INT_DEC, ANDROID_TYPED_VALUE_INT_HEX} else attr_value

            if version_name is not None and version_code is not None:
                return {
                    "version_name": normalize_android_version_name(version_name),
                    "version_code": normalize_android_version_code(version_code),
                }

        offset += chunk_size

    raise ValueError("Android version metadata was not found in the APK manifest.")


def extract_android_apk_version(payload):
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as apk:
            manifest = apk.read("AndroidManifest.xml")
    except KeyError:
        raise ValueError("Uploaded APK does not contain AndroidManifest.xml.") from None
    except zipfile.BadZipFile:
        raise ValueError("Uploaded APK is not a readable Android package.") from None

    try:
        return _extract_android_versions_from_binary_manifest(manifest)
    except ValueError as exc:
        raise ValueError(f"Could not read Android version metadata from the APK. {exc}") from None


def build_android_release_manifest(release, apk_url):
    if not release:
        return {
            "versionCode": 0,
            "versionName": "",
            "apkUrl": "",
            "notes": "",
        }
    return {
        "versionCode": int(release.get("version_code") or 0),
        "versionName": release.get("version_name") or "",
        "apkUrl": apk_url or "",
        "notes": release.get("notes") or "",
        "md5": release.get("md5") or "",
        "sizeBytes": int(release.get("size_bytes") or 0),
        "createdAt": release.get("created_at") or "",
    }


def build_android_apk_file_response(send_file_func, release, storage_path):
    response = send_file_func(
        str(storage_path),
        mimetype=release.get("content_type") or ANDROID_APK_CONTENT_TYPE,
        as_attachment=True,
        download_name=release.get("original_filename") or storage_path.name,
        conditional=False,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-MD5"] = release.get("md5") or ""
    response.headers["X-Android-Version-Code"] = str(release.get("version_code") or "")
    response.headers["X-Android-Version-Name"] = release.get("version_name") or ""
    return response


def build_android_apk_blob_response(send_file_func, release, payload):
    response = send_file_func(
        io.BytesIO(payload),
        mimetype=release.get("content_type") or ANDROID_APK_CONTENT_TYPE,
        as_attachment=True,
        download_name=release.get("original_filename") or "smart-water-tank.apk",
        conditional=False,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-MD5"] = release.get("md5") or ""
    response.headers["X-Android-Version-Code"] = str(release.get("version_code") or "")
    response.headers["X-Android-Version-Name"] = release.get("version_name") or ""
    return response
