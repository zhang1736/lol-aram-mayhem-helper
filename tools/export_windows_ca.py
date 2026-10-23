# Export the Windows ROOT/CA certificate stores to a PEM bundle.
#
# Why: pip ships its own certifi bundle, which does not trust the interception
# CA used on this machine. Python's ssl.create_default_context() reads the
# Windows store instead and works fine, so we hand that same trust set to pip
# via --cert / PIP_CERT.

import os
import ssl
import sys

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "certs")
OUT_FILE = os.path.abspath(os.path.join(OUT_DIR, "windows_ca_bundle.pem"))


def main():
    if not hasattr(ssl, "enum_certificates"):
        print("ssl.enum_certificates unavailable (non-Windows?)")
        return 1
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)

    seen = set()
    count = 0
    with open(OUT_FILE, "wb") as fh:
        for store in ("ROOT", "CA"):
            try:
                certs = ssl.enum_certificates(store)
            except Exception as exc:
                print("  skip store %s: %s" % (store, exc))
                continue
            for der, encoding, trust in certs:
                if encoding != "x509_asn":
                    continue
                if der in seen:
                    continue
                seen.add(der)
                try:
                    pem = ssl.DER_cert_to_PEM_cert(der)
                except Exception:
                    continue
                fh.write(pem.encode("ascii"))
                count += 1

    print("wrote %d certificates -> %s" % (count, OUT_FILE))
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())
