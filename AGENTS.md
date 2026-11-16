# Workspace conventions

- This is the active repository for new AgentOS work. Keep trace collection code in `motivation/trace`.
- Preserve raw attempts, including incomplete and incorrect tasks. Final answer correctness does not determine collection eligibility.
- Keep recording, real-tool offline qualification, and performance experiments distinct. Zero-delay qualification is not a performance result.
- Do not publish API keys, local profiles, raw recordings, downloaded datasets, reference solutions, model weights, or virtual environments. Generated artifacts belong in ignored directories.
- Do not silently weaken replay checks or substitute cached tool results. Keep resource observation gaps and missing metrics visible.
- Run `python motivation/trace/run_tests.py` after changing the recording toolkit. This suite uses local fixtures, not a model API.
