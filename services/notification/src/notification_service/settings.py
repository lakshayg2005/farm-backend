from functools import lru_cache

from farm_common.settings import ServiceSettings


class NotificationSettings(ServiceSettings):
    expo_push_url: str = "https://exp.host/--/api/v2/push/send"
    expo_access_token: str | None = None
    # Firebase service-account key (the JSON itself, never a committed file) for Android pushes.
    fcm_service_account_json: str | None = None
    fcm_project_id: str | None = None
    events_enabled: bool = True
    push_enabled: bool = True

    def fcm_client(self):
        """An FCM sender, or None when no service account is configured."""
        if not self.fcm_service_account_json:
            return None
        from notification_service.push import FcmClient

        return FcmClient(self.fcm_service_account_json, self.fcm_project_id)


@lru_cache
def get_notification_settings() -> NotificationSettings:
    return NotificationSettings()
