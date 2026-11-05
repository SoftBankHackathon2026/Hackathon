import hashlib
import hmac
import secrets

SCRYPT_PARAMS = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT_PARAMS)
    n, r, p = SCRYPT_PARAMS["n"], SCRYPT_PARAMS["r"], SCRYPT_PARAMS["p"]
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


UNKNOWN_USER_HASH = hash_password(secrets.token_urlsafe(16))
