---
name: rj-design
description: Use for RocJITsu feature design when comparing approaches, choosing an interface or ownership model, or testing a design with a prototype. Follow the requested stage, from options or an RFC through implementation.
---

# Design a RocJITsu change

Start with the decision and the constraints it must satisfy. Trace a representative caller through the existing path. Which behavior must survive, who owns the state, and which detail can stay private?

When the choice is open, compare materially different approaches against the same caller. Include extending the existing design when viable. Show how each would be used, what each costs, and what would make you choose differently. Give your recommendation in the requested format. An RFC should show example usage and explain the decisive tradeoff. When the user wants to choose, deliver the options and leave the decision with them.

Use a prototype when an unresolved question needs execution and the task includes trying it. Exercise the smallest end-to-end path that could disprove the design. For example, wire one real consumer to a proposed interface so its lifetime and error handling are visible. Report what the exercise settled and what remains open.

After trying the design, look again for an interface, layer or special case the caller can do without. Check the simpler version against the original requirements. If an RFC is part of the task, update its usage example and rationale to match what survived the prototype.
