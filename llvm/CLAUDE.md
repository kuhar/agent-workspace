# LLVM Project Organization

LLVM is a compiler framework, implemented in C++, Python, and C.

The conventional project checkout uses worktrees placed in `~/llvm`.
The project root is one of those worktrees, e.g., `~/llvm/main` or `~/llvm/test`.

For the exact project map, check the `project-map.md` file in the workspace directory. If it doesn't exist,
use the `/populate-project-map` command to create it.

## Code Style Conventions
- Prefer LLVM data structures over standard C++ ones (e.g., `SmallVector`, `DenseMap`, `DenseSet`)
- Prefer LLVM range functions over standard C++ algorithms and iterators (e.g., `llvm::sort`, `llvm::find`, `llvm::is_contained`)
  * You can find most of these in `llvm/include/llvm/ADT/STLExtras.h`
- Prefer range constructors / functions over variants that use iterators (e.g., `llvm::to_vector`, `llvm::to_vector_of<T>`, `llvm::make_filter_range`)
- Prefer `llvm::IsaPred<T>` over lambdas like `[](auto x) { return isa<T>(x); }`
- Uses LLVM/MLIR utilities extensively: `llvm::all_of`, `llvm::any_of`, `llvm::none_of`, `isa<>`, `cast<>`, `dyn_cast<>`
