import time

from invincible.core.settings import COOLDOWN_BASE_SECONDS, COOLDOWN_CAP_SECONDS


class ProviderHealth:
    def __init__(self):
        self.consecutive_failures = 0
        self.cooldown_until = None

class HealthTracker:
    def __init__(self):
        self.healths = {}
        self.disabled: set[str] = set()

    def get(self, provider_name: str) -> ProviderHealth:
        if provider_name not in self.healths:
            self.healths[provider_name] = ProviderHealth()
        return self.healths[provider_name]

    def record_failure(self, provider_name: str):
        health = self.get(provider_name)
        health.consecutive_failures += 1
        # 30s, 60s, 120s, 240s, ... capped
        cooldown_seconds = min(
            COOLDOWN_BASE_SECONDS * (2 ** (health.consecutive_failures - 1)),
            COOLDOWN_CAP_SECONDS,
        )
        health.cooldown_until = time.monotonic() + cooldown_seconds

    def record_success(self, provider_name: str):
        health = self.get(provider_name)
        health.consecutive_failures = 0
        health.cooldown_until = None

    def disable(self, provider_name: str):
        self.disabled.add(provider_name)
        health = self.get(provider_name)
        health.cooldown_until = None

    def is_available(self, provider_name: str) -> bool:
        if provider_name in self.disabled:
            return False
        health = self.get(provider_name)
        if health.cooldown_until is None:
            return True
        return time.monotonic() > health.cooldown_until
