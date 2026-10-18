## ADDED Requirements
### Requirement: Isolated beta managed book reads
The platform SHALL allow only the exact beta managed job and dispatcher principals to use the
existing portfolio metadata/state GET routes for isolated synthetic experiment
books, without granting portfolio writes or direct metadata storage access.

#### Scenario: Exact managed role reads an experiment book
- **WHEN** the exact beta managed job or dispatcher role requests portfolio metadata or state
for an initialized synthetic isolated book with named experiment and approach
and the deterministic internal experiment portfolio ID
- **THEN** the application returns the original contract-valid metadata/state.

#### Scenario: Network-isolated GPU inference requires a dispatcher check
- **WHEN** a separately approved current-book Qwen request is ready to launch
- **THEN** only the exact beta dispatcher may read the initialized isolated book
metadata/state, while the GPU worker retains network isolation and no AWS credentials.

#### Scenario: Ordinary user book or another principal is rejected
- **WHEN** a managed caller requests an ordinary user book, uses a different
managed role name, spoofs experiment markers on a public-created book, or calls
a write or excluded descendant route
- **THEN** the resource/application authorization rejects the request.

### Requirement: Preserve higher-environment isolation and policy integrity
The new exception SHALL affect beta only. Gamma/prod policy documents SHALL remain
unchanged. Existing tool route permissions and the8192byte API policy limit with
512byte reserved headroom SHALL remain enforced.

#### Scenario: Higher deployment synthesis and broad identity policies
- **WHEN** policies are synthesized for all environments and tested with broad
identity policies
- **THEN** only the two exact beta managed reads are added, existing tool
permissions remain equivalent, and all policies retain the size reserve.

#### Scenario: Beta policy annotations are omitted without changing authorization
- **WHEN** the beta API policy is serialized within the existing size reserve
- **THEN** only optional Sid annotations are omitted; every Allow/Deny, action,
resource and condition remains, and higher-environment serialization is unchanged.
