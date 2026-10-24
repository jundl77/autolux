# L9C experiment tools

These drivers fit priors, run decoder sweeps, compare candidates and build probe
reports. Artifacts belong under the corpus's `models/l9c_campaign/` directory.

Keep experiment outputs separate from source annotations, frozen splits and
benchmark baselines. Candidate comparisons must reproduce their reference rows
before reporting improvements. Include transition quality and entry latency with
classification scores.

Scripts verify the training checkout they import. Consult each driver's arguments
before running it and serialize GPU work.
