"""Remove secrets from anything that is about to be stored, logged, traced or sent to a browser."""

import re
from typing import Any

# A key is sensitive when its name ENDS in one of these words (api_key, access_token, client_secret, password).
# Counts such as input_tokens or max_tokens are not secrets: masking them hid token usage from the agent.
SENSITIVE_KEY = re.compile(
    r"(?:^|[_\-.])(?:api[_\-]?key|key|token|secret|password|passwd|authorization|cookie|credentials?)$", re.IGNORECASE
)
# Credentials with a fixed shape, found in error messages, attributes and content the applications send.
SECRET_VALUE = re.compile(
    "|".join(
        [
            r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}",  # OpenAI, Stripe and similar
            r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}",
            r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",  # AWS access key ids
            r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",  # GitHub tokens
            r"\bgithub_pat_[A-Za-z0-9_]{22,}\b",  # GitHub fine-grained tokens
            r"\bxox[abposr]-[A-Za-z0-9-]{10,}",  # Slack tokens
            r"\bAIza[0-9A-Za-z_-]{35}\b",  # Google API keys
            r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # JSON Web Tokens
            # private key blocks; a block cut off by the size limit is masked to its end
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\Z)",
        ]
    )
)
MASK = "[redacted]"


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: MASK if isinstance(k, str) and SENSITIVE_KEY.search(k.lower()) and v not in (None, "") else redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list | tuple):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return SECRET_VALUE.sub(MASK, obj)
    return obj
