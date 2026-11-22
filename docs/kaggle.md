# Kaggle notes

## Internet and project setup

Enable internet for the notebook (Kaggle may require phone verification). Upload
this repository as a private dataset, then copy it into writable working storage:

```bash
cp -r /kaggle/input/plainbagel-archiver/arch-yt /kaggle/working/arch-yt
```

The notebook expects `/kaggle/working/arch-yt` or a `PLAINBAGEL_PROJECT`
environment variable. Running `pip install -e .` from a read-only input dataset
will fail.

## Secrets

Add these under **Add-ons → Secrets**:

- `IA_ACCESS_KEY`
- `IA_SECRET_KEY`

The example notebook reads them with `UserSecretsClient`. Do not paste keys into
notebook cells or commit them.

## Working directory and persistence

Kaggle sessions are time-limited and the working directory may not survive a
stopped session. The archiver is designed to recover:

- archive.org items are checked before re-downloading when local records indicate
  a previous attempt;
- remote uploads skip files whose checksums already match;
- `run --reconcile` checks archive.org for records still marked pending.

For faster resume, keep the `work` directory as a saved notebook output or an
attached dataset. Avoid using `/kaggle/temp` for state that must survive.

## Suggested first run

```bash
python -m plainbagel_archiver doctor
python -m plainbagel_archiver discover
python -m plainbagel_archiver run --limit 1 --only-id OjyPy_4eVdw
python -m plainbagel_archiver verify --only-id OjyPy_4eVdw
```

Inspect the item on archive.org before starting a larger batch. The default
`session_minutes = 600` is a best-effort budget: the current video may finish
after the deadline, and the next run continues from stored state.

## Disk pressure

Kaggle working storage is limited. `cleanup_after_upload = true` removes local
media after successful upload and verification. `min_free_gb` stops the runner
before it starts another video when free space is too low. Do not lower the
reserve below enough space for the largest master plus temporary mux output.
