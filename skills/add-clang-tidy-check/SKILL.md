---
name: add-clang-tidy-check
description: Creates a new Clang Tidy check in the LLVM project.
---

# Adding a New Clang-Tidy Check

## 1. Create the Check Implementation

**Files to create:**
- `src/clang-tools-extra/clang-tidy/<module>/<CheckName>Check.h` - Header with class declaration
- `src/clang-tools-extra/clang-tidy/<module>/<CheckName>Check.cpp` - Implementation

**Key components:**
```cpp
// Header
class MyCheck : public ClangTidyCheck {
public:
  MyCheck(StringRef Name, ClangTidyContext *Context)
      : ClangTidyCheck(Name, Context) {}
  void registerMatchers(ast_matchers::MatchFinder *Finder) override;
  void check(const ast_matchers::MatchFinder::MatchResult &Result) override;
  bool isLanguageVersionSupported(const LangOptions &LangOpts) const override;
};

// Implementation
void MyCheck::registerMatchers(MatchFinder *Finder) {
  Finder->addMatcher(/* matcher */.bind("name"), this);
}

void MyCheck::check(const MatchFinder::MatchResult &Result) {
  const auto *Node = Result.Nodes.getNodeAs<NodeType>("name");
  diag(Loc, "message") << FixItHint::CreateRemoval(Range);
}
```

## 2. Register the Check

**File:** `src/clang-tools-extra/clang-tidy/<module>/<Module>TidyModule.cpp`

```cpp
#include "MyCheck.h"
// ...
CheckFactories.registerCheck<MyCheck>("module-check-name");
```

**File:** `src/clang-tools-extra/clang-tidy/<module>/CMakeLists.txt`

Add `MyCheck.cpp` to the source list.

## 3. Create Tests (Test-Driven Development)

**File:** `src/clang-tools-extra/test/clang-tidy/checkers/<module>/check-name.cpp`

```cpp
// RUN: %check_clang_tidy -std=c++17-or-later %s module-check-name %t

// Mock types/classes needed for matching

void test_case() {
  // Code that triggers the check
  // CHECK-MESSAGES: :[[@LINE-1]]:8: warning: message
  // CHECK-MESSAGES: :[[@LINE-2]]:13: note: note message
  // CHECK-FIXES: expected fixed code
}

void test_negative() {
  // Code that should NOT trigger the check
}
```

**Build and run tests:**
```bash
ninja check-clang-extra-clang-tidy-checkers  # Run all clang-tidy tests
python3 bin/llvm-lit -v ../src/clang-tools-extra/test/clang-tidy/checkers/<module>/check-name.cpp
```

## 4. Add Documentation

**File:** `src/clang-tools-extra/docs/clang-tidy/checks/<module>/check-name.rst`

```rst
.. title:: clang-tidy - module-check-name

module-check-name
=================

Brief description of what the check does.

Example
-------

.. code-block:: c++

  // Before
  code_before();

Transforms to:

.. code-block:: c++

  // After
  code_after();
```

**File:** `src/clang-tools-extra/docs/clang-tidy/checks/list.rst`

Add entry alphabetically:
```rst
   :doc:`module-check-name <module/check-name>`, "Yes"
```

**File:** `src/clang-tools-extra/docs/ReleaseNotes.rst`

Under "New checks":
```rst
- New :doc:`module-check-name
  <clang-tidy/checks/module/check-name>` check.

  Brief description.
```

## 5. Test on Real Codebase

```bash
# Run without fixes to see diagnostics
python3 src/clang-tools-extra/clang-tidy/tool/run-clang-tidy.py \
  -p build \
  -clang-tidy-binary build/bin/clang-tidy \
  -checks='-*,module-check-name' \
  'path/.*\.cpp$' 2>&1 | tee diagnostics.txt

# Apply fixes and verify build
python3 ... -fix 'path/.*\.cpp$'
ninja -C build check-<target>
```

## Key Tips

1. **Start with tests** - Write test cases first to define expected behavior
2. **Keep matchers simple** - Do complex checks in `check()`, not in matchers
3. **Use `assert` for bound nodes** - If matcher succeeded, nodes should exist
4. **Handle macros gracefully** - Check `isMacroID()` before emitting fix-its
5. **Use notes for clarity** - Point to relevant locations with `DiagnosticIDs::Note`
6. **Minimize test cases** - Remove redundant tests that don't increase coverage
7. **Test on real code** - Run on actual codebase (e.g., MLIR) before finalizing
8. **Debug with AST dump** - When matching fails unexpectedly, dump the AST to see the actual structure:
   ```bash
   build/bin/clang -Xclang -ast-dump -fsyntax-only -std=c++17 test.cpp
   ```

## Files Summary

| Purpose | Path |
|---------|------|
| Check header | `src/clang-tools-extra/clang-tidy/<module>/<CheckName>Check.h` |
| Check impl | `src/clang-tools-extra/clang-tidy/<module>/<CheckName>Check.cpp` |
| Module registration | `src/clang-tools-extra/clang-tidy/<module>/<Module>TidyModule.cpp` |
| CMake | `src/clang-tools-extra/clang-tidy/<module>/CMakeLists.txt` |
| Test | `src/clang-tools-extra/test/clang-tidy/checkers/<module>/check-name.cpp` |
| Documentation | `src/clang-tools-extra/docs/clang-tidy/checks/<module>/check-name.rst` |
| Check list | `src/clang-tools-extra/docs/clang-tidy/checks/list.rst` |
| Release notes | `src/clang-tools-extra/docs/ReleaseNotes.rst` |
