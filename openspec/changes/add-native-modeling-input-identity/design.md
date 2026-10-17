The optional native_input_ref has source_type platform_crypto_hourly and points to
an already persisted shared research_input by input_id and checksum. It retains
coinbase-crypto-hourly-v1, original dataset_checksum, as_of/available_at, 3600-second
frequency, the ordered BTC-USD/ETH-USD universe and truthful synthetic-source status.
The same reference is embedded in immutable proposal, resolution and history bytes.

The ordinary snapshot form is unchanged. A null snapshot requires a valid native
reference, automatic_experiment_policy=true, synthetic=true and run/input links.
The application additionally conditions on isolated book ownership and revision.
Normal create/resolve handlers continue to require their existing snapshot and
explicit confirmation. No native source is inserted into a daily snapshot table.

A narrowly proved compatibility rule recognizes a union retaining the complete old
schema and conditional constraints whose discriminator was excluded by the old
closed object. The absent-discriminator branch repeats exact old constraints.
Unconditional tightening and conditions affecting formerly valid records remain
breaking. Previous valid fixtures still replay against the new schema.
