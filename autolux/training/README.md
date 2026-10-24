# Training and evaluation

This directory owns corpus preparation, hand labeling, model training and
backtesting. Local music, model artifacts and derived caches live in data/raveform;
RAVEFORM_DATA_DIR overrides that location.

Use `uv run autolux label <audio>` for annotations and
`uv run python autolux/training/run_eval_set.py --help` for benchmark options.
The nn package contains model, export, decoder and evaluation tools. Campaign
directories contain experiment drivers with explicit artifact and import checks.

Keep train/validation/test separation and benchmark exclusions intact. Generated
artifacts stay out of Git. Run GPU jobs serially. Tests live in autolux/tests.
