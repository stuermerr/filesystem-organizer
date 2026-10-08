# Establish Content Identity only when proof requires it

`0.1.0` establishes a full Content Identity only for Exact Duplicate selection,
Structural Relationship proof, or a streamed materialization read that records
separate Execution Evidence. Unique files that are merely preserved use a
tagged Metadata Observation in the immutable Plan. This avoids reading every
payload during scan while keeping every omission and Structural Union dependent
on full identity proof; later Execution Evidence never changes the Plan or the
evidence on which it was finalized.
