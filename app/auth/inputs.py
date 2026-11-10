"""Credential input policy shared by browser, legacy, and account routes."""

import re
from typing import Annotated

from pydantic import AfterValidator, Field

from app.request_limits import BoundedPayloadRoute


class AuthPayloadRoute(BoundedPayloadRoute):
    max_body_bytes = 16 * 1024
    body_limit_message = 'Authentication request is too large.'
    no_store = True


def _valid_unicode(value: str) -> str:
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Use valid Unicode characters.') from None
    return value


def _new_password(value: str) -> str:
    if re.search(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff]', value):
        raise ValueError('Passwords cannot contain control characters or invalid Unicode.')
    return value


# A wider verification limit preserves existing passphrases and accepts old
# control characters. New passwords have a smaller bound and stricter format.
ExistingPassword = Annotated[str, Field(max_length=1024), AfterValidator(_valid_unicode)]
LoginIdentifier = Annotated[str, Field(min_length=1, max_length=320), AfterValidator(_valid_unicode)]
LoginPassword = Annotated[ExistingPassword, Field(min_length=1)]
NewPassword = Annotated[str, Field(min_length=8, max_length=256), AfterValidator(_new_password)]
AccountName = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_valid_unicode)]
RefreshInput = Annotated[str, Field(max_length=256), AfterValidator(_valid_unicode)]
