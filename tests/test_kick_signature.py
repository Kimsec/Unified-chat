import base64
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from unified_chat.connectors.kick import build_kick_signature_payload, verify_kick_signature


class KickSignatureTest(unittest.TestCase):
    def test_verifies_valid_signature(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        raw_body = b'{"message_id":"1","content":"hello"}'
        headers = {
            "Kick-Event-Message-Id": "01TEST",
            "Kick-Event-Message-Timestamp": "2026-04-04T20:00:00Z",
        }
        signature_input = build_kick_signature_payload(
            headers["Kick-Event-Message-Id"],
            headers["Kick-Event-Message-Timestamp"],
            raw_body,
        )
        signature = private_key.sign(signature_input, padding.PKCS1v15(), hashes.SHA256())
        headers["Kick-Event-Signature"] = base64.b64encode(signature).decode("utf-8")

        verify_kick_signature(headers, raw_body, public_key_pem=public_key_pem)


if __name__ == "__main__":
    unittest.main()

