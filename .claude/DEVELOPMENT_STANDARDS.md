# Development Standards

**Purpose**: Coding standards and best practices specific to this codebase
**Last Updated**: 2025-10-21
**Status**: Living document - update as patterns emerge

---

## Code Organization

### Python Module Standards

**File Naming**: snake_case for all Python files
- ✅ `rag_processing.py`, `garbled_text_detection.py`
- ❌ `RagProcessing.py`, `garbled-text-detection.py`

**Module Size**: Target <1000 lines per file
- If module exceeds 1000 lines, consider extraction:
  - Example: `rag_processing.py` extracted to `garbled_text_detection.py`, `strikethrough_detection.py`

**Import Order** (PEP 8):
```python
# 1. Standard library
import asyncio
import re
from pathlib import Path
from typing import Optional, Set

# 2. Third-party
import fitz
import ebooklib

# 3. Local modules
from lib.rag_data_models import TextSpan, PageRegion
from lib.garbled_text_detection import detect_garbled_text
```

### TypeScript/Node.js Standards

**File Naming**: kebab-case for files, PascalCase for classes
- ✅ `zlibrary-api.ts`, `venv-manager.ts`
- ❌ `zlibraryApi.ts`, `VenvManager.ts`

**Exports**: Named exports preferred over default
```typescript
// ✅ Preferred
export class VenvManager { ... }
export function createVenv() { ... }

// ❌ Avoid
export default class VenvManager { ... }
```

**Project-level hook/helper scripts must use `.cjs`**: `package.json` has
`"type": "module"`, so Node treats any `.js` file inside the project as ESM.
A CommonJS script (one that calls `require()`) saved as `.js` fails with
`ReferenceError: require is not defined in ES module scope`. Name it `.cjs`
(e.g. `.claude/hooks/*.cjs`) to force CommonJS regardless of the `type` field.

---

## Documentation Standards

### Naming Conventions (Enforced!)

**kebab-case** for all documentation (except formal specs):
- ✅ `sous-rature-validation.md`
- ✅ `performance-optimization.md`
- ❌ `SOUS_RATURE_VALIDATION.md` (only for specs in docs/)

**SCREAMING_SNAKE_CASE** for formal specs and workflows:
- ✅ `docs/specifications/PDF_QUALITY_SPEC.md`
- ✅ `.claude/TDD_WORKFLOW.md`
- ❌ Used anywhere else

**Timestamps**: Required for session notes, research validations
- ✅ `2025-10-21-session-summary.md`
- ✅ `validation-2025-10-20.md`
- ❌ Living documents (use git history instead)

### Where to Document

See the documentation-guidelines paragraph in [AGENTS.md](../AGENTS.md) for the complete guide.

**Quick Reference**:
| Type | Location |
|------|----------|
| Session summary | `claudedocs/session-notes/YYYY-MM-DD-topic.md` |
| Research findings | `claudedocs/research/{topic}/findings.md` |
| Architecture decision | `docs/adr/ADR-NNN-Title.md` |
| Technical spec | `docs/specifications/FEATURE_SPEC.md` |
| Development guide | `.claude/TOPIC_WORKFLOW.md` |

---

## Testing Standards

### Test Naming

**Python** (pytest):
- File: `test_{module_name}.py`
- Class: `Test{FeatureName}`
- Function: `test_{specific_behavior}`

**Node.js** (Jest):
- File: `{module-name}.test.js`
- Describe: Feature or component name
- Test: Specific behavior

### TDD Workflow (RAG Features)

**MANDATORY for all RAG pipeline features**:

1. **Acquire real test PDF** with the feature you're implementing
2. **Create ground truth** JSON documenting expected outputs
3. **Write failing test** using real PDF (NO MOCKS for detection/extraction)
4. **Implement** until test passes
5. **Manual verification**: Side-by-side PDF vs output review
6. **Performance validation**: Check against budgets
7. **Regression check**: Run ALL real PDF tests

See: [TDD_WORKFLOW.md](TDD_WORKFLOW.md) for complete process

### Test Coverage Targets

- **Node.js**: 80% line coverage (current: 78%)
- **Python**: 85% line coverage (current: 82%)
- **Critical paths**: 100% coverage (RAG quality pipeline, download flow)

---

## Performance Standards

### Budgets (Hard Constraints)

Defined in: `test_files/performance_budgets.json`

**All operations must stay within budget** - failing performance tests blocks merge.

| Operation | Budget | Enforcement |
|-----------|--------|-------------|
| X-mark detection | <10ms/page | Automated test |
| Garbled detection | <2ms/region | Automated test |
| Search latency | <2s (p95) | Manual monitoring |
| RAG processing | <15s/page | Automated test |

### Optimization Principles

1. **Measure First**: Profile before optimizing
2. **Cache Aggressively**: Page-level caching for expensive operations
3. **Parallelize**: Use ProcessPoolExecutor for independent operations
4. **Pre-Filter**: Fast checks before expensive analysis

---

## Code Quality Standards

### Python (PEP 8 + Project-Specific)

**Docstrings**: Required for all public functions
```python
def detect_garbled_text(text: str, threshold: float = 0.75) -> bool:
    """
    Detect garbled or corrupted text using statistical analysis.

    Args:
        text: Text to analyze
        threshold: Confidence threshold (0.0-1.0), default 0.75

    Returns:
        True if text appears garbled above threshold

    Example:
        >>> detect_garbled_text("sdkfjh23894 @#$")
        True
    """
```

**Type Hints**: Required for function signatures
```python
# ✅ Good
def process_pdf(file_path: Path, output_format: str = 'markdown') -> str:

# ❌ Bad
def process_pdf(file_path, output_format='markdown'):
```

**Data Classes**: Prefer dataclasses over dicts for structured data
```python
# ✅ Good
@dataclass
class TextSpan:
    text: str
    formatting: Set[str]

# ❌ Bad
span = {'text': 'hello', 'formatting': set()}
```

### TypeScript

**Strict Mode**: Enabled in tsconfig.json
```json
{
  "strict": true,
  "noImplicitAny": true,
  "strictNullChecks": true
}
```

**Interface Over Type**: Prefer interfaces for object shapes
```typescript
// ✅ Preferred
interface BookDetails {
  title: string;
  author: string;
}

// ⚠️ Use for unions/primitives
type BookId = string | number;
```

---

## Error Handling Standards

### Python

**Pattern**: Specific exceptions, informative messages
```python
# ✅ Good
if not pdf_path.exists():
    raise FileNotFoundError(
        f"PDF not found: {pdf_path}\n"
        f"Ensure file exists and path is correct"
    )

# ❌ Bad
if not pdf_path.exists():
    raise Exception("File not found")
```

**Logging**: Use logging module, not print()
```python
import logging

# ✅ Good
logging.info(f"Processing PDF: {pdf_path}")
logging.error(f"OCR failed: {error}", exc_info=True)

# ❌ Bad
print(f"Processing {pdf_path}")
```

### TypeScript

**Pattern**: Custom error classes + logging
```typescript
// ✅ Good
export class PythonBridgeError extends Error {
  constructor(message: string, public details?: any) {
    super(message);
    this.name = 'PythonBridgeError';
  }
}

throw new PythonBridgeError(
  `Python bridge execution failed for ${fn}: ${error}`,
  { functionName: fn, originalError: error }
);
```

---

## Git Workflow Standards

### Branch Naming

**Pattern**: `{type}/{scope}-{description}`

**Types**:
- `feature/` - New functionality
- `fix/` - Bug fixes
- `docs/` - Documentation only
- `refactor/` - Code restructuring
- `test/` - Test additions/fixes

**Examples**:
- ✅ `feature/rag-pipeline-enhancements-v2`
- ✅ `fix/venv-manager-test-warnings`
- ✅ `docs/workspace-reorganization`

### Commit Messages (Conventional Commits)

**Format**: `{type}({scope}): {description}`

```bash
# ✅ Good
feat(rag): implement formatting preservation with span grouping
fix(tests): update Stage 2 test signatures for tuple return
docs(workspace): reorganize with kebab-case naming

# ❌ Bad
update code
fixed bug
improvements
```

See: [VERSION_CONTROL.md](VERSION_CONTROL.md) for complete guide

---

## Workspace Hygiene

### Temporary Files

**Rule**: Clean up temporary files before session end

```bash
# ❌ Bad - leaving temp files
/tmp/debug_*.png
/tmp/test_*.py
workspace_temp/

# ✅ Good - cleanup script
rm -f /tmp/debug_*.png /tmp/test_*.py
```

**Automated**: Add to pre-commit hook:
```bash
#!/bin/bash
# .git/hooks/pre-commit
# Check for common temp file patterns
if git diff --cached --name-only | grep -E "temp|debug|test_.*\.py$"; then
  echo "⚠️  WARNING: Temporary files in commit. Review carefully."
fi
```

### Documentation Archival

**Rule**: Archive session notes >30 days old

**Manual**:
```bash
# Move old session notes
mv claudedocs/session-notes/2024-*.md claudedocs/archive/2024-MM/
```

**Automated** (future):
```bash
# Run monthly
bash .claude/scripts/archive_old_docs.sh
```

---

## Self-Improvement Practices

### Feedback Codification Loop

**Pattern**: Extract lessons → Document in standards → Future sessions learn automatically

**Process**:
1. **Encounter issue/mistake** during development
2. **Document in META_LEARNING.md** with context and lesson
3. **Extract pattern** if generalizable
4. **Add to this file** (DEVELOPMENT_STANDARDS.md) or PATTERNS.md
5. **Reference in CLAUDE.md** so AI learns automatically

**Example**:
```markdown
# In META_LEARNING.md
## 2025-10-21: Workspace Disorganization
**Issue**: 52 files accumulated in claudedocs/ with no structure
**Root Cause**: No clear documentation guidelines, inconsistent naming
**Lesson**: Need explicit "where to document" guide in CLAUDE.md
**Pattern Extracted**: Document lifecycle (create → edit → archive)
**Codified In**: DEVELOPMENT_STANDARDS.md (this file)
```

### Quality Review Ritual

**Frequency**: Before major commits or end of significant features

**Checklist**:
- [ ] All tests passing (including real PDF tests)
- [ ] Performance budgets met
- [ ] Code follows patterns from PATTERNS.md
- [ ] Documentation updated (README, CLAUDE.md, ADRs as needed)
- [ ] No temporary files in commit
- [ ] Conventional commit message
- [ ] META_LEARNING.md updated with insights

### Reflection Questions (Monthly)

Add to `.claude/META_LEARNING.md`:

1. **What patterns emerged this month?** (code, workflow, collaboration)
2. **What mistakes were repeated?** (should be codified to prevent)
3. **What worked exceptionally well?** (should be documented and amplified)
4. **What slowed us down unnecessarily?** (friction points to remove)
5. **What technical debt accumulated?** (prioritize in ROADMAP)

---

## Serena Memory Patterns

### Memory Categories

**Use Serena `write_memory()` for**:
- **Decisions**: `decision_YYYYMMDD_{topic}` - Architectural choices, rationale
- **Blockers**: `blocker_YYYYMMDD_{issue}` - Obstacles with resolution plans
- **Lessons**: `lesson_YYYYMMDD_{insight}` - Learnings from mistakes
- **Checkpoints**: `checkpoint_YYYYMMDD_HHMM` - Session state snapshots
- **Session**: `current_session` - Active task, todos, next steps

**Example**:
```python
# After discovering architectural flaw
write_memory(
  "decision_20251018_stage2_independence",
  {
    "decision": "Stage 2 X-mark detection runs independently (not conditional)",
    "rationale": "Sous-rature PDFs have clean text with visual X-marks",
    "previousAssumption": "Only garbled text needs X-mark detection",
    "impact": "Fixes missed detections on clean philosophical texts",
    "relatedADR": "ADR-008"
  }
)
```

### Session Lifecycle

**Pattern**: /sc:load → Work → Checkpoints → /sc:save

**Session Start**:
```bash
/sc:load
# Reads: current_session memory
# Displays: Current task, roadmap priorities, blockers
# AI reads: ROADMAP.md, ARCHITECTURE.md
```

**During Work** (every 30 min or before risky operations):
```python
# Checkpoint important state
write_memory("checkpoint_20251021_1430", {
  "task": "Implementing span grouping",
  "progress": "65%",
  "completed": ["Design", "Unit tests"],
  "pending": ["Integration", "Real PDF validation"],
  "notes": "Found edge case with footnotes interrupting formatted runs"
})
```

**Session End**:
```bash
/sc:save
# Writes: current_session memory with full state
# Optionally generates: .claude/STATUS.md
```

See: research findings formerly at `claudedocs/research/claude-code-best-practices/findings.md`
(claudedocs pruned 2026-07-24 — retrieve from git history if needed)

---

## Naming Standards Summary

| Item | Standard | Examples |
|------|----------|----------|
| Python files | snake_case | `rag_processing.py` |
| TypeScript files | kebab-case | `zlibrary-api.ts` |
| Python classes | PascalCase | `TextSpan`, `PageRegion` |
| Python functions | snake_case | `detect_garbled_text()` |
| TypeScript classes | PascalCase | `VenvManager` |
| TypeScript functions | camelCase | `downloadBook()` |
| Test files (Python) | test_{module}.py | `test_rag_processing.py` |
| Test files (TS) | {module}.test.js | `zlibrary-api.test.js` |
| Documentation | kebab-case | `sous-rature-validation.md` |
| Specifications | SCREAMING_SNAKE | `PDF_QUALITY_SPEC.md` |
| Workflows (.claude/) | SCREAMING_SNAKE | `TDD_WORKFLOW.md` |

---

## Quality Gates

### Pre-Commit (Automated)

Required checks before commit:
- [ ] All tests passing (`npm test` + `uv run pytest`)
- [ ] No linting errors
- [ ] No temporary files in staged changes
- [ ] Conventional commit message format

### Pre-Merge (Manual Review)

Required before merging to master:
- [ ] All quality gates passed
- [ ] Real PDF tests passing (for RAG features)
- [ ] Performance budgets met
- [ ] Documentation updated
- [ ] CODE_REVIEW.md checklist completed

### Pre-Release (CI/CD)

Required before version bump:
- [ ] Full test suite passing
- [ ] Integration tests passing
- [ ] Build succeeds on clean checkout
- [ ] Documentation complete and current

---

## Project-Specific Patterns

### RAG Pipeline Development

**Always**:
- ✅ Use real PDFs for testing (no synthetic mocks for extraction)
- ✅ Create ground truth JSON before implementation
- ✅ Validate against performance budgets
- ✅ Manual side-by-side verification

**Never**:
- ❌ Skip real PDF validation
- ❌ Lower performance budgets to make tests pass
- ❌ Use fragile heuristics (user will push back!)
- ❌ Assume text patterns (use robust metadata/bbox analysis)

### Python Bridge Communication

**Always**:
- ✅ Use PythonShell for Node↔Python communication
- ✅ Validate Python script paths at runtime
- ✅ Handle Python errors gracefully with informative messages
- ✅ Return structured data (JSON), not raw text

**Never**:
- ❌ Assume Python scripts are in specific locations
- ❌ Suppress Python stderr (contains valuable error info)
- ❌ Return massive strings through bridge (use file paths)

---

## Maintenance Rituals

### Daily (Per Session)
- **Start**: Read ROADMAP.md + ARCHITECTURE.md + `/sc:load`
- **Work**: Follow PATTERNS.md, update TodoWrite
- **End**: `/sc:save`, clean temp files

### Weekly (Sunday)
- **Update ROADMAP.md**: Next week's priorities (10 min)
- **Review ARCHITECTURE.md**: Update if major changes (5 min)
- **Archive old docs**: Move session notes >30 days (5 min)
- **Total**: ~20 min/week

### Monthly (First Sunday)
- **META_LEARNING reflection**: Answer 5 reflection questions
- **ISSUES.md review**: Close resolved, prioritize open
- **Test coverage check**: Ensure meeting targets
- **Dependency updates**: Review and update if needed
- **Total**: ~1 hour/month

---

## Anti-Patterns (What NOT to Do)

❌ **Flat Directory Dumps**: Don't put 50+ files in one directory
→ **Solution**: Use subdirectories (session-notes/, research/, etc.)

❌ **Mixed Naming Conventions**: SCREAMING + snake_case + kebab-case in same place
→ **Solution**: Enforce kebab-case for docs, see naming table above

❌ **Redundant Markers**: "FINAL", "COMPLETE", "V2" in filenames
→ **Solution**: Use directory structure + git history for versioning

❌ **No Timestamps on Session Notes**: Can't determine freshness
→ **Solution**: Always use YYYY-MM-DD prefix for session notes

❌ **Documentation Without Context**: "analysis.md", "notes.md", "report.md"
→ **Solution**: Descriptive names: "rag-pipeline-analysis.md"

❌ **Temporary Files Committed**: debug.sh, test_output.txt, temp/
→ **Solution**: Clean before commit, add to .gitignore

❌ **Fragile Heuristics**: Pattern matching that only works for one case
→ **Solution**: Robust metadata/bbox analysis, statistical methods

❌ **Skipping Real PDF Tests**: Using only synthetic/mocked tests for RAG
→ **Solution**: TDD workflow requires real PDFs with ground truth

---

## Success Metrics

**Development Velocity**:
- Can resume work after interruption in <5 minutes
- Can find relevant documentation in <2 minutes
- Can understand current task/plan/architecture in <10 minutes

**Code Quality**:
- >80% test coverage maintained
- Performance budgets met consistently
- Zero regressions in real PDF tests

**Documentation Health**:
- <10 files in claudedocs/session-notes/ (rest archived)
- 100% kebab-case compliance
- All living docs updated within 14 days

**Self-Improvement**:
- META_LEARNING.md updated monthly
- Patterns extracted and codified when discovered
- Quality gates prevent repeated mistakes

---

## References

- **Coding Patterns**: [PATTERNS.md](PATTERNS.md)
- **TDD Process**: [TDD_WORKFLOW.md](TDD_WORKFLOW.md)
- **Git Workflow**: [VERSION_CONTROL.md](VERSION_CONTROL.md)
- **Quality Framework**: [RAG_QUALITY_FRAMEWORK.md](RAG_QUALITY_FRAMEWORK.md)
- **Session State System**: research formerly in `claudedocs/research/claude-code-best-practices/` (in git history)
