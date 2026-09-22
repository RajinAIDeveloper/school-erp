import hashlib
from django.core.cache import cache
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

class RateLimitedAuthenticationForm(AuthenticationForm):
    def clean(self):
        username=str(self.data.get("username","")).casefold()
        address=self.request.META.get("REMOTE_ADDR","") if self.request else ""
        digest=hashlib.sha256((address+":"+username).encode()).hexdigest()
        key="login-attempt:"+digest
        if cache.get(key,0)>=5:
            raise ValidationError("Too many attempts. Try again in 15 minutes.")
        try:
            data=super().clean()
        except ValidationError:
            cache.add(key,0,900)
            try: cache.incr(key)
            except ValueError: cache.set(key,1,900)
            raise
        cache.delete(key)
        return data
