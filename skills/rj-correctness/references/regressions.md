# Choose a regression test

- Use an existing gtest or CTest fixture when it exercises the contract locally. Use the RocJITsu corpus when the failure needs a real kernel, library call or hardware comparison. A compiler warning or packaging failure may be cheapest to verify with the original build or install command.
- Drive the normal entry point. Derive the expected result independently of the implementation. Run the same check before and after the fix. If the baseline cannot run it, record why and give the available failing evidence.
- For a composite kernel such as Guitar Hero, feed each distinguishing result into a nearby checkpoint. Revert one fix at a time in an isolated checkout and confirm which checkpoint first diverges. This lets one compact reference output identify individual faults.
- For fuzzing or coverage, match target, instrumentation, inputs and execution budget. Compare newly reached paths and explain what keeps the remaining ones out of reach. Measure decode and execution coverage separately.
