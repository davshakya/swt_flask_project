from flask_app.device_key_vault import decrypt_device_key, encrypt_device_key


def test_device_key_vault_round_trip_is_encrypted_and_device_key_is_not_plaintext():
    device_key = "unique-device-key-with-more-than-32-characters"
    ciphertext = encrypt_device_key(device_key, "permanent-app-secret")

    assert ciphertext
    assert device_key not in ciphertext
    assert decrypt_device_key(ciphertext, "permanent-app-secret") == device_key


def test_device_key_vault_rejects_wrong_app_secret_and_tampering():
    ciphertext = encrypt_device_key("device-secret", "correct-app-secret")

    assert decrypt_device_key(ciphertext, "wrong-app-secret") == ""
    replacement = "A" if ciphertext[len(ciphertext) // 2] != "A" else "B"
    tampered = ciphertext[: len(ciphertext) // 2] + replacement + ciphertext[len(ciphertext) // 2 + 1 :]
    assert decrypt_device_key(tampered, "correct-app-secret") == ""
