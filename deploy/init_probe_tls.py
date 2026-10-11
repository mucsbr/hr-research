"""Authorized remote only: generate an actual internal CA and SAN server certificate.

The public CA is included in one-time pairing configuration; clients verify TLS normally.
Existing keys are never overwritten. This does not create native signing certificates.
"""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def save(path, data, mode):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    with os.fdopen(descriptor, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def private_key(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server-url", required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Generate TLS only in the authorized remote project environment")
    url = urlsplit(args.server_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.path or url.query:
        parser.error("An explicit HTTPS origin is required")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = [args.output / name for name in ("ca.key", "ca.pem", "server.key", "server.pem")]
    if any(p.exists() for p in paths):
        parser.error("Existing TLS material must be inspected; it will not be overwritten")
    clock = datetime.now(timezone.utc)
    ca_key, server_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hr-research internal probe CA")])
    ca = (x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(clock - timedelta(minutes=5))
          .not_valid_after(clock + timedelta(days=730)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
          .sign(ca_key, hashes.SHA256()))
    try:
        san = x509.IPAddress(ipaddress.ip_address(url.hostname))
    except ValueError:
        san = x509.DNSName(url.hostname)
    server = (x509.CertificateBuilder()
              .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, url.hostname)]))
              .issuer_name(issuer).public_key(server_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(clock - timedelta(minutes=5)).not_valid_after(clock + timedelta(days=365))
              .add_extension(x509.SubjectAlternativeName([san]), False)
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
              .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), False)
              .sign(ca_key, hashes.SHA256()))
    for path, data, mode in zip(paths, [private_key(ca_key), ca.public_bytes(serialization.Encoding.PEM),
                                      private_key(server_key), server.public_bytes(serialization.Encoding.PEM)],
                                [0o600, 0o644, 0o600, 0o644]):
        save(path, data, mode)
    print(json.dumps({"origin": args.server_url, "ca_sha256": ca.fingerprint(hashes.SHA256()).hex(),
                      "private_keys_printed": False, "internal_ca": True}))


if __name__ == "__main__":
    main()
