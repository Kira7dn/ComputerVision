# Training workspace

Offline dataset preparation, model training and validation live outside the LS-Vision runtime.

```text
training/
├─ models/   # Model contract validation tools
├─ vendor/   # Ignored upstream source checkouts
└─ runs/     # Ignored local training outputs and experiments
```

Deployable model artifacts belong in `assets/models/`; application runtime code belongs in
`apps/src/`. `vendor/` and `runs/` are local, reproducible inputs/outputs and are not committed.
