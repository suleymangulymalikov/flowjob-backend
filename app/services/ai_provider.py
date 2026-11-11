"""Shared OpenAI transport, structured response handling, and error policy."""

from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from app.config import settings


class AiError(Exception):
    """Base class for failures safe to translate into an API response."""


class AiNotConfigured(AiError):
    pass


class AiTimeout(AiError):
    pass


class AiRateLimited(AiError):
    pass


class AiInvalidResponse(AiError):
    pass


class AiProviderError(AiError):
    pass


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int


# Model families that reject a sampling temperature (reasoning models). Others accept it.
_NO_TEMPERATURE_PREFIXES = ('o1', 'o3', 'o4', 'gpt-5')


def supports_temperature(model: str) -> bool:
    return not model.strip().lower().startswith(_NO_TEMPERATURE_PREFIXES)


def request_analysis[T: BaseModel](
    system_prompt: str,
    user_input: str,
    schema: type[T],
    *,
    max_output_tokens: int | None = None,
    timeout: float | None = None,
    temperature: float | None = None,
) -> tuple[T, Usage]:
    if not settings.openai_api_key:
        raise AiNotConfigured()

    import openai

    request: dict = {
        'model': settings.openai_model,
        'input': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_input},
        ],
        'text_format': schema,
        'max_output_tokens': max_output_tokens or settings.openai_max_output_tokens,
        'store': False,
    }
    if temperature is not None and supports_temperature(settings.openai_model):
        request['temperature'] = temperature

    try:
        # A timeout or disconnect can occur after the provider starts generating.
        # One attempt avoids submitting that same potentially billable request again.
        with openai.OpenAI(
            api_key=settings.openai_api_key, timeout=timeout or settings.openai_timeout_seconds, max_retries=0,
        ) as client:
            try:
                response = client.responses.parse(**request)
            except openai.BadRequestError as error:
                # A model that rejects `temperature` fails validation BEFORE generating anything, so repeating the
                # request without it is not a second billable generation. Any other 400 is a real failure.
                if 'temperature' not in request or 'temperature' not in str(error).lower():
                    raise
                del request['temperature']
                response = client.responses.parse(**request)
    except openai.APITimeoutError as error:
        raise AiTimeout() from error
    except openai.RateLimitError as error:
        raise AiRateLimited() from error
    except (ValidationError, openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as error:
        raise AiInvalidResponse() from error
    except openai.APIError as error:
        raise AiProviderError() from error

    parsed = response.output_parsed
    if parsed is None:
        raise AiInvalidResponse()
    usage = response.usage
    return parsed, Usage(
        input_tokens=int(getattr(usage, 'input_tokens', 0) or 0),
        output_tokens=int(getattr(usage, 'output_tokens', 0) or 0),
    )


def error_response(error: AiError) -> tuple[int, str]:
    if isinstance(error, AiNotConfigured):
        return 503, 'The AI service is not configured yet.'
    if isinstance(error, AiTimeout):
        return 504, 'The analysis timed out. Please try again.'
    if isinstance(error, AiRateLimited):
        return 429, 'The AI service is busy. Please try again shortly.'
    if isinstance(error, AiInvalidResponse):
        return 502, 'The AI service returned an unusable result.'
    return 502, 'The AI service is unavailable. Please try again.'


def estimate_cost_usd(usage: Usage) -> float:
    return round(
        usage.input_tokens / 1_000_000 * settings.openai_input_price_per_1m
        + usage.output_tokens / 1_000_000 * settings.openai_output_price_per_1m,
        6,
    )
