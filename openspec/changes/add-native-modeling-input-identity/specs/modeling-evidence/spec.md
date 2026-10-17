## ADDED Requirements

### Requirement: Honest native input identity
An automatic isolated synthetic experiment decision that consumes the supported
platform crypto hourly dataset SHALL retain its original shared input reference,
source checksum, dataset checksum, ordered universe, frequency and availability
timestamps. It SHALL NOT manufacture a daily snapshot ID or attribute unrelated
snapshot data to the decision. A null snapshot ID SHALL require this native reference
and the explicit isolated experiment policy with run and input links.

#### Scenario: Native hourly policy reaches the paper ledger
- **WHEN** a captured native BTC/ETH policy is executed at a later eligible quote
- **THEN** the atomic decision, resolution and history SHALL share the verified
  native identity and preserve the existing authoritative portfolio revision

### Requirement: Existing snapshot and confirmation integrity
Ordinary user portfolio decisions SHALL retain their approved snapshot requirement
and explicit confirmation. Invalid native provenance, future availability, incomplete
hourly candles, unsupported universes and synthetic forward prices SHALL be rejected
before an authoritative ledger revision is published.

#### Scenario: Unsupported native source attempts a ledger update
- **WHEN** a native source fails its provenance or timing checks
- **THEN** no portfolio revision or successful execution receipt SHALL be committed
