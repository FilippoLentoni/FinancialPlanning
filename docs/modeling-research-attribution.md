# Constituent news attribution

`finplan-constituent-attribution/1` attributes a retrieved headline to a searched
constituent only when it contains a security name derived from that run's dated
published universe or explicit `$TICKER` / `EXCHANGE:TICKER` syntax. Only complete source security names and their legal-suffix-normalized
forms are accepted; partial first words or personal names are not invented aliases. Generic names and all unadorned ticker tokens are conservative
unverified candidates. A headline containing ordinary A, IT, ON, ALL or ARE words
does not establish stock coverage. Missing coverage is retained rather than
converted to neutral evidence.

Each original headline remains unchanged. `symbol_attribution` records the
version, method, dated universe checksum/as-of, matched original content/security
name/worker and excluded token matches with reasons. Deduplication unions these
provenance records while retaining the individual worker findings. Coverage
records link eligible news IDs and attribution methods, and separately retain
excluded news IDs. A matched headline without a valid publication time remains
quarantined and does not contribute eligible coverage.

This additive correction changes no HTTP tasks, four-worker concurrency, source
rights, provider budgets, policy input shapes or frozen model artifacts. It does
not reclassify or rewrite historical captured inputs. Older RSS ticker-token
attribution lacks this identity proof and must remain visibly unverified while
its exact original content is still inspectable. Raw-source attribution metadata
and the exact selected model context are separate evidence layers; omitted
provenance is not retroactively inserted into a model's original input.
