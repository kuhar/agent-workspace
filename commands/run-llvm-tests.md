# Run Tests (LLVM)

## Overview

Build all the required targets and run tests.

## Steps

1. **Find the build directory and `cd` to it**
  - This will usually be `WORKSPACE_ROOT/build`

2. **Identify the relevant subproject to test**
  - Explore the codebase looking for relevant tests
  - For mlir, run `ninja check-mlir`
    * For more specific components, append the component path test target, e.g.,: `ninja check-mlir-dialect-spirv`
  - For ADT, run `ninja ADTTests && unittests/ADT/ADTTests --gtest_filter="<TEST_PREFIX>*"`
  - For Support, run `ninja SupportTests && unittests/Support/SupportTests --gtest_filter="<TEST_PREFIX>*"`
  - For clang-tidy checks, run `ninja check-clang-extra-clang-tidy-checkers`

3. Make sure that all test targets are actually built -- use `ninja`.

4. **Summarize results**
  - How many tests were discovered. If none, the test invocation was most likely wrong.
  - How many passed / failed / were disabled

5. **Analyze failures (if any)**
    - Categorize by type: pre-existing / unrelated, new failures
    - Generate a repro command that runs the failing tests only
