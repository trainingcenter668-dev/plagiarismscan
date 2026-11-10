# PlagiaScan validation report
- `python -m unittest discover -s tests`: 12 tests passed (engine, API routes, local library, full scan pipeline with mocked providers).
- Live provider calls were not run in the build sandbox (no internet). Use `/diagnose` on your machine.
