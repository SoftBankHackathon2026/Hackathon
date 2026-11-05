import hashlib
import json


def sha256_json(value: object, *, compact: bool = False) -> str:
    separators = (",", ":") if compact else None
    encoded = json.dumps(value, sort_keys=True, separators=separators).encode()
    return hashlib.sha256(encoded).hexdigest()
