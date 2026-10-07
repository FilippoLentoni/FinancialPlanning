# Rollback procedure

Task 10.3. Design: D6 and Migration Plan step 5. Spec: environment-promotion, "Rollback recording
and procedure" (ENV-11), and cross-repo-ownership, "Backward-compatible publish before consumer
update".

> **Drill status.** The gamma rollback drill (ENV-11) needs deployed pipelines, so it is
> **blocked** until the bootstrap (task 10.5) and the first releases exist. The procedure and its
> pre-checks below can be reviewed and run on fixture manifests today.

## Principles

- **Redeploy, never rebuild.** A rollback redeploys the stored cloud assembly and image digests
  of a previously recorded `release_id` through the pipeline's normal deploy actions and scoped
  deploy role.
- **Every rollback is recorded.** It writes a new manifest. Nothing is edited in place.
- **Data is never rolled back.** Records are immutable, and schema changes follow
  expand-then-contract, so an older release can still read data written by a newer one.
- **A contract version is never deleted.** A contract rollback means consumers re-pin to an
  earlier version, while producers keep serving the prior majors.
- **A failed deploy inside one stack update** is undone by CloudFormation's automatic rollback.
  That needs no manual procedure, and no new manifest is written for a deploy that never
  completed.

## Where the information is

| What | Where |
|---|---|
| Current release | `/finplan/<env>/<repo>/release/current-release-id` |
| Current manifest (includes `previous_release_id`) | `/finplan/<env>/<repo>/release/manifest` |
| Earlier manifests | SSM parameter history of the manifest parameter, plus copies in the repo's pipeline artifact store (the rollback ledger) |
| Stored assembly for a release | Pipeline artifact store, keyed by `release_id` |

## Procedure

1. **Pick the target.** Usually it is the current manifest's `previous_release_id`. For example,
   prod smoke fails after `rel_Y`, and the previous release is `rel_X`. Read the target's manifest
   from the ledger and confirm that its `artifact_digest` exists in the artifact store.
2. **Check contract compatibility.** Rolling back must not remove a contract major that a
   consumer in the same environment still pins. Build the candidate set, which is the target
   manifest plus the other repositories' current manifests in that environment, and run the
   end-to-end gate:

   ```sh
   finplan-conformance manifest-gate --env <env> <dir-with-candidate-manifests>
   # or read the other repos' manifests read-only from SSM:
   finplan-conformance manifest-gate --env <env> --from-ssm
   ```

   - **COMPATIBLE**: continue.
   - **INCOMPATIBLE / BLOCKED**: a consumer pins a major that the target no longer serves. This
     requires a **coordinated rollback**. Roll back or re-pin the named consumers first (the
     reverse of the integration order: agent, then tools, then model, then platform), then repeat
     this step.
3. **Run the rollback.** Start the pipeline with the variable `rollback_to_release_id` set to the
   target (`rel_X`). The pipeline redeploys the stored assembly for that release, and only to the
   environment(s) being rolled back. For prod, the manual approval stage still applies, and the
   approver and timestamp are recorded.
4. **Write the rollback manifest.** The deploy writes a new manifest for the environment:
   - `release_id` = `rel_X` (the release now running)
   - `artifact_digest` = the digest recorded for `rel_X` (unchanged, because there was no rebuild)
   - `previous_release_id` = `rel_Y` (the release that was replaced)
   - `rolled_back_from` = `rel_Y`
   - `deployed_at` = now; in prod, also `approved_by` and `approved_at`

   It also updates `current-release-id` to `rel_X`. A synthetic example is
   `fixtures/release-manifest/valid/prod-rollback.json`.
5. **Verify.** Run the environment's tests: smoke for prod, the gamma suite for gamma. Then run
   `manifest-gate` again for that environment.
6. **Follow up.** Fix forward with a new commit, which gets a new `release_id`. The failed
   release `rel_Y` stays in the ledger and is never deleted or overwritten.

## Contract-package rollback

If a contract release causes the problem:

- Do not unpublish it; published versions are immutable, and the publish step refuses overwrites.
- Consumers re-pin to the earlier version and digest, then redeploy through their pipelines.
- If the bad release was a new major, producers keep serving the previous major, so consumers
  pinned to it are unaffected.
- A fix is published as a new version (patch or minor). For a breaking fix, it is a new major
  with a migration record.

## Gamma rollback drill (ENV-11, blocked by bootstrap)

Once the pipelines exist:

1. Deploy two consecutive releases to gamma.
2. Roll gamma back to the first one with `rollback_to_release_id`.
3. Verify that the new gamma manifest has `release_id` equal to the first release,
   `rolled_back_from` and `previous_release_id` equal to the second, and an unchanged
   `artifact_digest`, and that the gamma suite passes.
4. Run a second drill where a consumer still pins a major that the rollback target does not
   serve. Verify that `manifest-gate` reports the conflicting consumer and that the rollback
   requires a coordinated rollback.
