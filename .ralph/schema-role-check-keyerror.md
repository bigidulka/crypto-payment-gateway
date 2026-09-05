# Runtime schema-role check KeyError — local correction

Date: 2026-09-05
Status: diagnostic and local-only correction. No production prepare retry, DDL, role, credential, runtime env, service switch, restart, push, or deployment occurred after the blocked check.

## One guarded remote diagnostic

The committed remote script at `02dd4316084452a6454563ce3417311626865832` was loaded through a read-only wrapper and called with the same `check` arguments. The wrapper emitted only exception class and traceback locations (basename/function/line), no exception arguments, source env values, URLs, credentials, script text, proxy values, or raw stderr.

```text
KeyError
prepare_runtime_schema_role.py | main | line 839
```

The same wrapper then read only:

```text
revision=0009_merchant_rails
arbitron_runtime exists=false
API/worker image IDs unchanged from pre-preparation capture
```

## Root cause

`check()` returned safe source/backup/gate metadata but omitted `phase`. `main()` always serialized `result["phase"]`, producing `KeyError` after all check gates succeeded. This was a script result-shape bug—not an external service mapping, Docker metadata format, source-manifest, database, privilege, or wrapper invocation failure.

## Minimal correction

`check()` now returns `"phase": Phase.CHECKED`. Added a regression which supplies the same safe check result shape to `main()` and asserts values-free output:

```text
{"phase":"checked","source_sha":"<expected-sha>"}
```

No other phase logic, grants, migration, raw env behavior, accepted `0010` migration, or provider/financial behavior changed.

## Next boundary

The correction is local-only and awaits targeted test review plus separate director authorization before another production `check` invocation. The existing external source manifest and protected pre-DDL backup remain preserved; no cleanup/retry was performed.
