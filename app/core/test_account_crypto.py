"""Hybrid public-key sealing of a program's test account (login id + password).

The API holds only the public key (`TEST_ACCOUNT_PUBLIC_KEY`) and can seal but never open.
Only the worker holds the private key (`TEST_ACCOUNT_PRIVATE_KEY`), so a decryptable secret
still exists only in the worker environment.

Scheme (v1):
- a fresh random AES-256-GCM data key and 96-bit nonce per seal encrypt the JSON
  `{"login_id", "password"}`;
- the data key is wrapped with RSA-OAEP (SHA-256, MGF1-SHA-256), RSA >= 3072 bits;
- associated data `metaphor:test-account:v1|<program_id>|<version>|<key_id>` is bound into
  both the GCM tag and the OAEP label, so a row copied to another program, replayed under
  another version, or relabelled with another key id fails to open;
- `key_id` is the SHA-256 hex of the public key's DER SubjectPublicKeyInfo.

ponytail: anyone holding the (public) key can seal a valid account; DB write access already
implies that. Integrity against the DB itself is out of scope.
"""

import hashlib
import json
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCHEME = "metaphor:test-account:v1"
MIN_RSA_BITS = 3072
GENERATED_RSA_BITS = 3072
_NONCE_BYTES = 12


class AccountKeyError(Exception):
    """Key missing or unusable. The message never includes key material."""


class AccountDecryptError(Exception):
    """A sealed account failed to open (tampered, swapped, or wrong key)."""


@dataclass(frozen=True, repr=False)
class Sealed:
    key_id: str
    wrapped_key: bytes
    ciphertext: bytes  # nonce || AES-GCM ciphertext+tag


@dataclass(frozen=True)
class Credentials:
    login_id: str
    password: str

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"


def _pem(value: str) -> bytes:
    # Env files often hold a PEM on one line with literal "\n".
    return value.strip().replace("\\n", "\n").encode()


def load_public_key(pem: str) -> rsa.RSAPublicKey:
    try:
        key = serialization.load_pem_public_key(_pem(pem))
    except (ValueError, TypeError):
        raise AccountKeyError("invalid public key") from None
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < MIN_RSA_BITS:
        raise AccountKeyError("public key must be RSA >= 3072 bits")
    return key


def load_private_key(pem: str) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(_pem(pem), password=None)
    except (ValueError, TypeError):
        raise AccountKeyError("invalid private key") from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < MIN_RSA_BITS:
        raise AccountKeyError("private key must be RSA >= 3072 bits")
    return key


def key_id(public_key: rsa.RSAPublicKey) -> str:
    der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(der).hexdigest()


def associated_data(program_id: str, version: int, kid: str) -> bytes:
    return f"{SCHEME}|{program_id}|{version}|{kid}".encode()


def _oaep(label: bytes) -> padding.OAEP:
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=label)


def seal(
    public_key: rsa.RSAPublicKey, program_id: str, version: int, credentials: Credentials
) -> Sealed:
    kid = key_id(public_key)
    ad = associated_data(program_id, version, kid)
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(
        {"login_id": credentials.login_id, "password": credentials.password}
    ).encode()
    ciphertext = nonce + AESGCM(data_key).encrypt(nonce, plaintext, ad)
    return Sealed(kid, public_key.encrypt(data_key, _oaep(ad)), ciphertext)


def open_sealed(
    private_key: rsa.RSAPrivateKey, program_id: str, version: int, sealed: Sealed
) -> Credentials:
    kid = key_id(private_key.public_key())
    if sealed.key_id != kid:
        raise AccountDecryptError("KEY_ID_MISMATCH")
    ad = associated_data(program_id, version, kid)
    try:
        data_key = private_key.decrypt(sealed.wrapped_key, _oaep(ad))
        nonce, body = sealed.ciphertext[:_NONCE_BYTES], sealed.ciphertext[_NONCE_BYTES:]
        value = json.loads(AESGCM(data_key).decrypt(nonce, body, ad))
        return Credentials(login_id=value["login_id"], password=value["password"])
    except (ValueError, InvalidTag, KeyError, TypeError):
        raise AccountDecryptError("DECRYPT_FAILED") from None


def generate_keypair() -> tuple[str, str]:
    """(private PEM, public PEM) for a new RSA-3072 keypair."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=GENERATED_RSA_BITS)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem
