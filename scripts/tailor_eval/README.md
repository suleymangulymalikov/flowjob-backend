# Tailoring baseline

Measures what CV tailoring actually produces, how long it takes and what it costs, on 8 fixed test cases
(`cases.json`, all invented people). Run it before and after changing the prompt, model or pipeline, and
compare the two result files.

1. Start the backend as usual (real `OPENAI_API_KEY` in `.env` for a real measurement, `mock` for a dry run).
2. From the backend folder: `uv run python scripts/tailor_eval/tailor_eval.py`
3. Results are saved in `scripts/tailor_eval/results/` (ignored by git).

Each case runs a match, tailoring from that match, and tailoring straight from the job post: up to 24 AI calls
for all 8 cases, a few cents at the default model. Use `--limit 2` for a quick try. It creates two throwaway
accounts (`tailor-eval-...@flowjob.dev`) in the database the backend uses, so point the backend at a
development database.
