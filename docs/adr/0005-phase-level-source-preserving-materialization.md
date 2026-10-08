# Use phase-level durability for source-preserving materialization

The normal `0.1.0` path builds one owned partial destination without per-file
durability, flushes the destination filesystem once, publishes the whole tree
atomically with no-replace semantics, and synchronizes the destination parent
before reporting success. Recovery uses coarse Materialization Attempts, a
durable manifest, and the staging root's filesystem identity; incomplete owned
staging is restarted instead of resumed per file. This deliberately trades
forensic destination verification and exact resume for substantially lower I/O
while the source remains unchanged and available for a safe retry.
