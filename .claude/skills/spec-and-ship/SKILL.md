# Spec-and-Ship — Full Product Lifecycle Skill

**Trigger**: User says "/spec-and-ship" or asks to "spec and ship" a feature/task list.

## Process

### Phase 1: Spec
1. Write a detailed spec for each task/feature to `tasks/specs/<feature>.md`
2. Include: motivation, affected files, API contracts, edge cases, test plan

### Phase 2: Codex Review of Spec
3. Send spec to Codex via cross-agent for review
4. Codex returns: dependency-aware implementation plan, risk flags, suggested batch ordering
5. CC incorporates feedback, finalizes spec

### Phase 3: Implementation (Agent Swarms)
6. Group tasks into dependency-aware batches
7. Launch parallel CC subagents for independent tasks within each batch
8. Sequential batches wait for predecessors to complete
9. Each agent: implement, write tests, verify passing

### Phase 4: Codex Review of Implementation
10. Send completed work to Codex for code review
11. Codex returns: discrepancies, bugs, style issues, missing edge cases
12. CC fixes issues, sends back to Codex
13. **Repeat until consensus**: both CC and Codex agree the work is correct

### Phase 5: Ship
14. Run full test suite (`python -m pytest --tb=short -q`)
15. Run linter (`python -m ruff check .`)
16. Update `CONTINUATION_PROMPT.md` with new state
17. Commit with descriptive message
18. Push to remote

## Rules
- Never skip the Codex review phases — consensus is required
- Each pass-back must include specific line references
- Spec must exist before implementation begins
- Tests must pass before shipping
- CONTINUATION_PROMPT.md must reflect the new reality
