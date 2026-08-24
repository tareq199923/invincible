"""Replay a captured 400 payload directly against its provider, bypassing
the gateway.

Rescued from the Phase 13.5 root-cause hunt: running the exact outgoing
payload outside Invincible is what proved a TokenRouter 400 was actually a
provider-side 403 ("no access to model ...") wrapped by the aggregator.

Usage:
    1. Set INVINCIBLE_DEBUG_400=1 and reproduce the failing request once;
       this creates debug_400_<provider>_<epoch>.json in the working dir.
    2. python tools/replay_payload.py [path-to-dump.json]

Without an argument, the LATEST debug_400_*.json dump is replayed. The
provider base URL and api_key_env come from the dump's provider name via
invincible.providers.yaml; the key itself is read from your environment.
"""
import glob
import json
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()


def latest_dump() -> str | None:
    dumps = sorted(glob.glob("debug_400_*.json"))
    return dumps[-1] if dumps else None


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else latest_dump()
    if not path or not os.path.isfile(path):
        print(f"no dump found ({path!r}). Reproduce with INVINCIBLE_DEBUG_400=1.")
        return 2

    from invincible.core.config import load_providers_config
    from invincible.core.settings import settings

    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    provider_name = data["provider"]
    payload = data["payload"]
    payload.pop("stream", None)

    provider = next(
        (
            p
            for p in load_providers_config(
                settings.config_path()
            )["providers"]
            if p["name"] == provider_name
        ),
        None,
    )
    if provider is None:
        print(f"provider {provider_name!r} not in current configuration")
        return 2
    api_key = settings.provider_api_key(provider["api_key_env"])
    if not api_key:
        print(f"set {provider['api_key_env']} to replay against this provider")
        return 2

    url = f"{provider['base_url']}{provider.get('chat_path', '/chat/completions')}"
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    params = None
    if provider.get("auth_type") == "query":
        headers.pop("Authorization", None)
        params = {provider.get("auth_param", "key"): api_key}

    resp = httpx.post(url, headers=headers, params=params, json=payload,
                      timeout=90)
    print("STATUS", resp.status_code)
    print(resp.text[:800])
    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
