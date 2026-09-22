"""
SMS backends. Select with settings.SMS_BACKEND.

- ConsoleSMSBackend: prints messages to the terminal (development).
- HttpSMSBackend: generic HTTP GET gateway; most Bangladeshi providers (BulkSMSBD,
  SSL Wireless, Alpha SMS...) accept api_key / senderid / number / message query params.
  Configure URL, key, sender ID and extra params under Settings -> SMS gateway.
"""
import json
import logging
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

log = logging.getLogger(__name__)


class BaseSMSBackend:
    def send(self, message):
        raise NotImplementedError

    def deliver(self, message):
        from .models import SMSMessage
        try:
            response = self.send(message)
            message.status = SMSMessage.Status.SENT
            message.provider_response = str(response)[:2000]
            message.sent_at = timezone.now()
        except Exception as exc:  # noqa: BLE001 - provider failures must never crash the request
            log.exception("SMS send failed")
            message.status = SMSMessage.Status.FAILED
            message.provider_response = str(exc)[:2000]
        message.save(update_fields=["status", "provider_response", "sent_at", "updated_at"])
        return message.status == SMSMessage.Status.SENT


class ConsoleSMSBackend(BaseSMSBackend):
    def send(self, message):
        print(f"[SMS -> {message.phone}] {message.body}")
        return "console"


class HttpSMSBackend(BaseSMSBackend):
    def send(self, message):
        school = message.school
        if not school.sms_api_url:
            raise RuntimeError("SMS gateway URL is not configured (Settings -> SMS gateway).")
        params = {
            "api_key": school.sms_api_key,
            "senderid": school.sms_sender_id,
            "number": message.phone,
            "message": message.body,
        }
        for pair in school.sms_extra_params.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k.strip()] = v.strip()
        url = f"{school.sms_api_url}?{urlencode(params)}"
        req = Request(url, headers={"User-Agent": "SchoolERP/1.0"})
        with urlopen(req, timeout=15) as resp:  # noqa: S310 - URL configured by the school administrator
            body = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
            code = str(data.get("response_code", data.get("status", "")))
            if code and code not in ("202", "200", "success", "SUCCESS", "OK"):
                raise RuntimeError(f"Gateway rejected message: {body}")
        except json.JSONDecodeError:
            pass
        return body


def get_backend():
    return import_string(getattr(settings, "SMS_BACKEND", "messaging.backends.ConsoleSMSBackend"))()
