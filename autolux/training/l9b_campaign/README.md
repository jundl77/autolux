# L9B experiment tools

These drivers prepare hand-labeled tracks, validate their corpus admission,
extract features, fit priors, sweep decoder settings and produce evaluation
reports. Artifacts belong under the corpus's `models/l9b_campaign/` directory.

Run preparation and artifact checks before extraction or training. Split changes
are explicit operations with backups; validation/test membership and benchmark
exclusions must remain intact. Do not modify published labels or benchmark baselines.

Scripts that use another training checkout verify its import source. Consult each
driver's arguments before running it and serialize GPU work.
