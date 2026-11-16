"""Shows why OpenAI calls fail, using exactly the key and model the backend uses. Makes one tiny request.

    uv run python scripts/tailor_eval/check_openai.py

Never prints the key. Run it from the backend folder.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import openai  # noqa: E402

from app.config import settings  # noqa: E402

key = settings.openai_api_key or ''
print('Model the backend uses :', settings.openai_model)
print('Key comes from         :', 'a Windows environment variable (this overrides .env)'
      if os.environ.get('OPENAI_API_KEY') else 'the .env file')
print('Key looks like         :', f'{key[:5]}... ({len(key)} characters)' if key else 'EMPTY')
if key in ('', 'mock'):
    sys.exit('No real key is configured.')

client = openai.OpenAI(api_key=key, timeout=30, max_retries=0)
try:
    reply = client.responses.create(model=settings.openai_model, input='Reply with the single word: ok',
                                    max_output_tokens=16, store=False)
    print('\nRESULT: OpenAI works with this key and model. Reply:', (reply.output_text or '').strip())
    if '--models' in sys.argv:
        skip = ('audio', 'realtime', 'image', 'tts', 'transcribe', 'search', 'embedding', 'moderation', 'instruct')
        names = sorted(m.id for m in client.models.list()
                       if m.id.startswith(('gpt', 'o1', 'o3', 'o4', 'chatgpt')) and not any(w in m.id for w in skip))
        print('\nText models this key can use:')
        print('\n'.join(names) or '(none listed)')
except openai.APIError as error:
    print(f'\nRESULT: OpenAI refused the request -> {type(error).__name__} '
          f'(status {getattr(error, "status_code", "n/a")})')
    print(str(error)[:500])
    try:
        names = sorted(m.id for m in client.models.list() if m.id.startswith('gpt') and 'audio' not in m.id
                       and 'realtime' not in m.id and 'image' not in m.id and 'tts' not in m.id
                       and 'transcribe' not in m.id and 'search' not in m.id)
        print('\nThe key itself is valid. Text models it can use:')
        print(', '.join(names) or '(none listed)')
    except openai.APIError as listing:
        print(f'\nListing models also failed ({type(listing).__name__}), so the key itself is the problem.')
