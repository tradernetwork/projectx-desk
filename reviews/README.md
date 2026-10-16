# Reviews

`review-*.md` are independent reviews of this repo, each written by a different model and named for it:

- `review-deepseek-v4-flash.md`: DeepSeek V4 Flash
- `review-gemini-3.8-flash.md`: Gemini 3.8 Flash
- `review-gpt-6.md`: GPT-6
- `review-codex-2026-10-16.md`: Codex, against commit `db1be93`. Seven findings, all reproduced with its embedded script and fixed (see Round 7 in [../docs/DECISIONS.md](../docs/DECISIONS.md)); regression tests are in `tests/test_px_codex_review.py`.

They are unverified opinions. Nobody has checked their claims line by line, and the models read the code, not a real gateway. Some
findings led to changes (see "Round 3" in [../docs/DECISIONS.md](../docs/DECISIONS.md)); others have not been acted on.

`REVIEW_PROMPT.md` is the prompt given to each model.
