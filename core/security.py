import hashlib
import ipaddress

from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.core.exceptions import ValidationError


def client_ip(request):
    """
    The address a request really came from.

    Behind a proxy every request arrives from the proxy, so a limit keyed on REMOTE_ADDR
    would count the whole school as one visitor and lock everyone out together. Each trusted
    proxy appends the address it saw to X-Forwarded-For, so the visitor is the entry
    TRUSTED_PROXY_COUNT places from the end. Entries before that were written by the visitor
    and are never believed.
    """
    remote = request.META.get("REMOTE_ADDR", "")
    hops = getattr(settings, "TRUSTED_PROXY_COUNT", 0)
    if hops > 0:
        chain = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
        if len(chain) >= hops:
            try:
                return str(ipaddress.ip_address(chain[-hops]))
            except ValueError:
                pass
    return remote


class RateLimitedAuthenticationForm(AuthenticationForm):
    def clean(self):
        username = str(self.data.get("username", "")).casefold()
        address = client_ip(self.request) if self.request else ""
        digest = hashlib.sha256((address + ":" + username).encode()).hexdigest()
        key = "login-attempt:" + digest
        if cache.get(key, 0) >= 5:
            raise ValidationError("Too many attempts. Try again in 15 minutes.")
        try:
            data = super().clean()
        except ValidationError:
            cache.add(key, 0, 900)
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, 900)
            raise
        cache.delete(key)
        return data


def safe_next(request, address, fallback):
    """
    Where to send someone back to: the posted address when it stays on this site, and the
    view's own destination otherwise. A form's "next" field is whatever the browser sent, and
    an unchecked one turns this site into a link that forwards people to a lookalike.
    """
    from django.shortcuts import resolve_url
    from django.utils.http import url_has_allowed_host_and_scheme

    if address and url_has_allowed_host_and_scheme(
        address, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return address
    return resolve_url(fallback)
