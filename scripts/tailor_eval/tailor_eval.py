"""Baseline run for CV tailoring quality, speed and cost.

Sends each test case in cases.json through the RUNNING local backend twice:
  path A: job match first, then tailoring from that match (what most users do)
  path B: tailoring straight from the job post (the AI reads the full post)
and saves every suggestion, the dropped-proposal counts, seconds and cost to results/.

Usage (backend must be running, normally on http://localhost:8000):
    uv run python scripts/tailor_eval/tailor_eval.py
    uv run python scripts/tailor_eval/tailor_eval.py --limit 2      # only the first 2 cases
    uv run python scripts/tailor_eval/tailor_eval.py --base http://localhost:8000/api/v1

With a real OPENAI_API_KEY this makes up to 3 paid AI calls per case (24 for all 8 cases, a few cents in total
at the default model). It creates two throwaway accounts named tailor-eval-...@flowjob.dev in whatever database the
backend is using. It only talks to the backend over HTTP and never reads .env.
"""
import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Api:
    def __init__(self, base: str):
        self.base, self.token = base.rstrip('/'), None

    def call(self, method: str, path: str, body=None, timeout: float = 150):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        seconds = round(time.perf_counter() - started, 2)
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode('utf-8', 'replace')
        return status, parsed, seconds

    def register(self, label: str) -> str:
        email = f'tailor-eval-{int(time.time())}-{label}@flowjob.dev'
        status, body, _ = self.call('POST', '/auth/register', {
            'name': f'Tailor Eval {label.upper()}', 'email': email, 'password': secrets.token_urlsafe(18) + 'aA1!'})
        if status >= 400:
            sys.exit(f'Could not create the test account ({status}): {body}')
        self.token = body.get('accessToken')
        return email


def brief(draft: dict) -> dict:
    """The parts of a tailoring draft worth comparing between runs."""
    suggestions = draft.get('suggestions') or []
    ops: dict[str, int] = {}
    for s in suggestions:
        ops[s.get('op', 'rewrite')] = ops.get(s.get('op', 'rewrite'), 0) + 1
    return {
        'kept': len(suggestions), 'byType': ops, 'proposalStats': draft.get('proposalStats'),
        'tokens': draft.get('tokens'), 'costUsd': draft.get('estimatedCostUsd'),
        'missingQualifications': draft.get('missingQualifications'), 'questions': draft.get('questions'),
        'suggestions': [{k: s.get(k) for k in ('op', 'section', 'original', 'suggested', 'skill', 'explanation',
                                               'jobRequirement', 'evidence', 'risk', 'newTerms') if s.get(k) is not None}
                        for s in suggestions],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', default='http://localhost:8000/api/v1')
    parser.add_argument('--limit', type=int, default=0, help='run only the first N cases')
    parser.add_argument('--yes', action='store_true', help='skip the confirmation question')
    args = parser.parse_args()

    cases = json.loads((HERE / 'cases.json').read_text(encoding='utf-8'))
    if args.limit:
        cases = cases[:args.limit]
    print(f'{len(cases)} cases, up to {len(cases) * 3} AI calls against {args.base}')
    if not args.yes and input('With a real OpenAI key this costs money (a few cents). Continue? [y/N] ').strip().lower() != 'y':
        sys.exit('Stopped. Nothing was sent.')

    a, b = Api(args.base), Api(args.base)
    try:
        accounts = [a.register('a'), b.register('b')]
    except urllib.error.URLError as error:
        sys.exit(f'Cannot reach the backend at {args.base}. Is it running? ({error.reason})')

    rows, mock_like = [], 0
    for case in cases:
        row: dict = {'id': case['id'], 'note': case['note'], 'jobTitle': case['jobTitle']}
        job = {'jobTitle': case['jobTitle'], 'company': case.get('company'), 'jobDescription': case['jobDescription']}
        print(f'\n[{case["id"]}] {case["note"]}')

        # Path A: match, then tailor from the match.
        status, cv, _ = a.call('POST', '/cvs', {'name': case['id'], 'kind': 'original', 'content': case['cv']})
        if status >= 400:
            row['error'] = f'create CV {status}: {cv}'
            rows.append(row); print('  ', row['error']); continue
        status, match, seconds = a.call('POST', '/matches', {'cvId': cv['id'], **job})
        row['match'] = {'status': status, 'seconds': seconds}
        if status < 400:
            diagnostics = match.get('diagnostics') or {}
            row['match'] |= {'score': match.get('score') if match.get('scoreAvailable', True) else None,
                             'verdict': match.get('verdict'), 'noScoreReason': match.get('scoreUnavailableReason'),
                             'requirements': [{k: r.get(k) for k in ('requirement', 'category', 'status', 'explanation')}
                                              for r in match.get('requirements') or []],
                             # How often the backend threw away what the AI said because a quote was not word-for-word:
                             'evidenceQuotesDropped': diagnostics.get('citations_dropped', 0),
                             'downgradedToNotShown': diagnostics.get('statuses_downgraded', 0),
                             'requirementsRejected': match.get('rejected') or [],
                             'droppedQuotes': diagnostics.get('droppedCitations') or [],
                             'tokens': match.get('tokens'), 'costUsd': match.get('estimatedCostUsd')}
            # The app now sends the post along with the match (the backend checks it is the same post, never stores it).
            status, draft, seconds = a.call('POST', '/tailor', {'matchId': match['id'], 'cvId': cv['id'],
                                                                'jobDescription': case['jobDescription']})
            row['tailorFromMatch'] = {'status': status, 'seconds': seconds, **(brief(draft) if status < 400 else {'error': draft})}
        else:
            row['match']['error'] = match
        m = row['match']
        if m['status'] >= 400:
            print(f'   match: FAILED {m["status"]} {m.get("error")}')
        else:
            print(f'   match: {m.get("score")}% {m.get("verdict")} in {m["seconds"]}s, {len(m.get("requirements") or [])} requirements, '
                  f'{m.get("downgradedToNotShown", 0)} downgraded to "not shown", {len(m.get("requirementsRejected") or [])} rejected')

        # Path B: tailor straight from the job post (second account, so nothing is cached or shared).
        status, cv_b, _ = b.call('POST', '/cvs', {'name': case['id'], 'kind': 'original', 'content': case['cv']})
        if status < 400:
            status, draft, seconds = b.call('POST', '/tailor', {'cvId': cv_b['id'], **job})
            row['tailorDirect'] = {'status': status, 'seconds': seconds, **(brief(draft) if status < 400 else {'error': draft})}

        for key, label in (('tailorFromMatch', 'from match'), ('tailorDirect', 'from job post')):
            t = row.get(key)
            if not t:
                continue
            if t['status'] >= 400:
                print(f'   tailor {label}: FAILED {t["status"]} {t.get("error")}')
                continue
            if t.get('tokens') == {'input': 640, 'output': 360}:
                mock_like += 1
            stats = t.get('proposalStats') or {}
            print(f'   tailor {label}: kept {t["kept"]} of {stats.get("proposed", "?")} proposed {t["byType"]} '
                  f'dropped {stats.get("rejected", {})} in {t["seconds"]}s, ${t.get("costUsd")}')
            for example in stats.get('rejectedExamples') or []:
                print(f'      dropped: {example.get("op")} at {example.get("path")} ({example.get("reason")})')
        rows.append(row)

    def total(key: str, field: str) -> float:
        return sum((r.get(key) or {}).get(field) or 0 for r in rows)

    summary = {
        'cases': len(rows),
        'matchSecondsAvg': round(total('match', 'seconds') / max(len(rows), 1), 1),
        'matchScores': {r['id']: (r.get('match') or {}).get('score') for r in rows},
        'matchDowngradedToNotShown': int(total('match', 'downgradedToNotShown')),
        'matchRequirementsRejected': sum(len((r.get('match') or {}).get('requirementsRejected') or []) for r in rows),
        'tailorFromMatch': {'keptAvg': round(total('tailorFromMatch', 'kept') / max(len(rows), 1), 1),
                            'secondsAvg': round(total('tailorFromMatch', 'seconds') / max(len(rows), 1), 1)},
        'tailorDirect': {'keptAvg': round(total('tailorDirect', 'kept') / max(len(rows), 1), 1),
                         'secondsAvg': round(total('tailorDirect', 'seconds') / max(len(rows), 1), 1)},
        'costUsdTotal': round(total('match', 'costUsd') + total('tailorFromMatch', 'costUsd') + total('tailorDirect', 'costUsd'), 4),
        'looksLikeMockAi': mock_like > 0,
    }
    if rows and all((r.get('match') or {}).get('status', 500) >= 400 for r in rows):
        print('\nEvery AI call failed. Run this to see why:  uv run python scripts/tailor_eval/check_openai.py')
    out_dir = HERE / 'results'
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f'tailor_eval_{time.strftime("%Y%m%d_%H%M%S")}.json'
    out.write_text(json.dumps({'base': args.base, 'accounts': accounts, 'summary': summary, 'cases': rows},
                              indent=2, ensure_ascii=False), encoding='utf-8')
    print('\nSummary:', json.dumps(summary, indent=2))
    if mock_like:
        print('\nNOTE: these results look like the MOCK AI (OPENAI_API_KEY=mock). Put the real key in .env and restart '
              'the backend to measure real quality.')
    print(f'\nSaved to {out}')


if __name__ == '__main__':
    main()
