# FinancialPlanning

FinancialPlanning is the shared planning platform repository of the finplan system. Four public
repositories make up the system:

| Repository | Role |
|---|---|
| **FinancialPlanning** (this repo) | Planning platform: storage, plan lifecycle API, ingestion, scheduler, and the cross-repo **contract package** |
| FinanceModel | Research workspace, SageMaker jobs, model registry, job interface |
| FinanceLambdasTool | MCP adapter Lambdas (tools) |
| FinanceAgent | AgentCore Runtime and Gateway, explanation provider configuration |

Each repository has its own pipeline and promotes independently through beta, gamma and prod.
The repositories share identifiers, schemas and resource references only through the contract
package published from this repository.

## Where to start

- [`contracts/README.md`](contracts/README.md): the contract package. It covers the ownership
  matrix, identifiers, environments, pipelines, decisions and open questions, and links to the
  procedures in [`contracts/docs/`](contracts/docs/).
- [`openspec/`](openspec/): specifications and change proposals. The current baseline is
  [`openspec/changes/establish-cross-repo-contracts/`](openspec/changes/establish-cross-repo-contracts/),
  with the proposal, design (decisions D1 to D12), specs and tasks.

## Public-repository hygiene

This repository is public. It never contains AWS account IDs, ARNs that name an account, bucket,
role or connection names, endpoint URLs, secret values or real market data. Placeholders such as
`<account-id>` are used instead. Resources are resolved at deploy time through SSM parameters and
release manifests. The leak scan enforces this:

```sh
cd contracts/python && uv run finplan-conformance leak-scan ..
```

Local configuration, such as the bootstrap account check file, lives outside the repository
(`~/.finplan/`). It is also covered by [`.gitignore`](.gitignore).
