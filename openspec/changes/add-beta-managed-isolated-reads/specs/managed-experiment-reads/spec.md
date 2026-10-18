## ADDED Requirements
### Requirement: Isolated beta managed book reads
The platform SHALL allow only the exact beta managed job principal to use the
existing portfolio metadata/state GET routes for isolated synthetic experiment
books, without granting portfolio writes or direct metadata storage access.

#### Scenario: Exact managed role reads an experiment book
- **WHEN** the exact beta managed job role requests portfolio metadata or state
for a synthetic isolated book with named experiment and approach
- **THEN** the application returns the original contract-valid metadata/state.

#### Scenario: Ordinary user book or another principal is rejected
- **WHEN** a managed caller requests an ordinary user book, uses a different
managed role name, or calls a write or excluded descendant route
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
