# Refreshing an existing port

Read this when `.car/app` already exists and the application source has changed.

Run the same preparation command with `--refresh` and the same import root:

```bash
python3 <skill_dir>/prepare.py <import-root> .car --refresh
```

The initial copy records source-file hashes in
`.car/config/.porting-state.json`. Refresh compares three states:

```text
previous source hash → current source
                    ↘ current .car/app
```

- Only the source changed: update `.car/app`.
- Only `.car/app` changed: preserve the port edit.
- The source added a path unused by the port: add it.
- The source deleted an unmodified path: delete it from `.car/app`.
- Both sides changed the same path differently: make no changes and report all
  conflicts.

Resolve a conflict in `.car/app`, then either make the source match that result
or intentionally start over. The script does not guess a merge because an
adapter and its source often change for different reasons while sharing one
module.

`--force` is not refresh. It discards the entire `.car/app` tree and replaces it
with a clean source copy while retaining `.car/config`. Use it only when every
adapter and workflow edit in `.car/app` is intentionally disposable.

After refresh, survey changed imports, dependencies, runtime assets, and service
boundaries again, then run the full validator. A clean file merge is not proof
that the deployment contract still holds.

