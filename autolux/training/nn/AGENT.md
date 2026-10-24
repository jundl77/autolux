# Model training and decoding

Train and evaluate section and downbeat models against explicit data splits.
Use the shared nine-class vocabulary and corpus definitions. Preserve artist
separation, loss masks, benchmark exclusions and validation/test boundaries.

Artifacts must record model identity, preprocessing geometry and provenance.
Inference is deterministic; ONNX exports must retain variable sequence length.
The runtime consumes exported artifacts and a fixed-lag decoder, not training code
with mutable experiment state.

Keep selection on validation data and report test measurements separately.
Distinguish measured live rhythm from expert-grid diagnostic bounds. Never change
labels, splits or benchmark baselines to make a result pass.

Resolve assets through `corpus_root`. Keep generated artifacts in the ignored
corpus. Scripts using another checkout must assert which training package they
import. Run GPU jobs serially and test changes under `autolux/tests/`.
