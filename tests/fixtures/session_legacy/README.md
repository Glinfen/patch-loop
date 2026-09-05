# Legacy Session Runtime Fixture

This fixture is intentionally stored as the pre-SRF JSON/checkpoint/trace
shape. It is a read-only compatibility input for SRF-00; later migration tests
must consume these files instead of rebuilding the data with the new schema.

Key identities:

- completed task: `legacy-completed-task`
- running task: `legacy-running-task`
- confirmed write: `legacy-write-call`
- checkpoint step: `1`
- memory cursor: `tool:legacy-write-call`
- cache epoch: `legacy-epoch`
