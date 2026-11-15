# Qualify AMDGPU instruction behavior

Record the target, instruction bytes, operand form, wave size and relevant mode bits. Choose evidence for the claim under investigation.

| Claim | Check |
| --- | --- |
| Specified behavior | Read the generation's manual under `$WORK_ROOT/amdgpu-isa-manuals` and AMD's published XML under `shared/machine-readable-isa/isa`. Check provenance before citing an entry. Local deltas and legacy supplements derived from LLVM count as LLVM evidence. Cite the manual section and encoding. Identify reserved forms and undefined inputs. |
| LLVM encoding | Check the target's MC tests and instruction definitions under `$LLVMS/main`. Assemble or disassemble the bytes for that target. Account for aliases and operand restrictions. A round-trip establishes LLVM's encoding choice. |
| Simulator execution | Inspect the decoded operands. Run a kernel whose inputs distinguish the competing behaviors. Compare expected and observed values in the affected and neighboring lanes or registers. |
| Hardware execution | Run the same defined case, initialization and observation on the matching device. Record its revision. A neighboring GPU qualifies only the case on that GPU. When matching hardware is unavailable, label the target result simulator-only and cite the source for its expected value. |

For a comprehensive audit, inventory the requested forms and check relevant changes between adjacent generations. Separate missing support from wrong behavior. When the manual, implementation or proposed patch disagree, identify a probe that can distinguish their predictions.

For generated code, fix the canonical input and run the working checkout's `emulation/rocjitsu/scripts/generate-amdisa.sh`. Inspect the generated diff across affected targets and isolate unrelated regeneration from the patch.
