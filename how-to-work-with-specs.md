# How to Work with Specs — Spec-Driven Development Guide

## The Workflow (in order)

For every new feature, bug fix, or change, follow this exact sequence:

### 1. `/speckit.specify` — Create the spec
Describe what you want to build or change. The spec will be created in `specs/###-feature-name/spec.md`.

**Always reference existing docs:**
- `docs/05-database.md` for table structures
- `docs/04-backend.md` for endpoints
- `docs/03-frontend.md` for pages and components

### 2. `/speckit.clarify` — Resolve ambiguities *(recommended)*
Before planning, clarify anything unclear. Catches gaps early. Run before `/speckit.plan`.

### 3. `/speckit.plan` — Create the technical plan
Generates `plan.md` with research, data model, and contracts. Must pass the constitution check defined in `.specify/memory/constitution.md`.

### 4. `/speckit.checklist` — Quality checklist *(optional)*
Validates requirements completeness, clarity, and consistency before generating tasks.

### 5. `/speckit.tasks` — Break into actionable tasks
Generates `tasks.md` — small, ordered, verifiable tasks. Each references exact file paths.

### 6. `/speckit.analyze` — Cross-check consistency *(optional)*
Validates spec ↔ plan ↔ tasks ↔ constitution. Detects contradictions, gaps, and unverified claims. Run after `/speckit.tasks` and before `/speckit.implement`.

### 7. `/speckit.implement` — Execute
**Only run this after all above phases pass and you've explicitly authorized implementation.** This is the only phase that modifies code.

### 8. `/speckit.converge` — Assess & append
After implementation, assesses what was completed and adds any remaining work as new tasks.

---

## Key Rules

| Rule | Why |
|------|-----|
| **Never skip spec → plan → tasks** | Prevents impulsive, untracked code changes |
| **Always reference existing docs** | `docs/` is your single source of truth |
| **One feature per spec** | Keeps changes small, isolated, and reviewable |
| **Check constitution compliance** | `.specify/memory/constitution.md` defines all non-negotiable rules |
| **Don't change the stack** | React 19, FastAPI, Supabase, TON — these are fixed |
| **Minimum modification rule** | Only change what's strictly necessary; preserve existing behavior |
| **Commit after each change** | Convention: `type: short description` (e.g., `fix:`, `feat:`, `ux:`) |
| **Update docs & CHANGELOG** | Always update relevant docs and `docs/CHANGELOG.md` with changes |

---

## Quick Example

```
User: "I want to add a dark mode toggle"

1. /speckit.specify → "Add dark mode toggle to Artemisa frontend
   - Toggle in navigation bar
   - Persist preference in localStorage
   - Respect system preference on first visit"

2. /speckit.clarify → Resolves:
   - "Should it persist per device or per user account?" → localStorage
   - "System preference or manual only?" → Both, with manual override

3. /speckit.plan → Technical approach:
   - CSS custom properties for color scheme
   - Tailwind dark: prefix for conditional styles
   - React context for theme state
   - localStorage for persistence

4. /speckit.tasks → Generates:
   - T001: Create ThemeContext in hooks/useTheme.ts
   - T002: Add CSS variables for light/dark themes
   - T003: Build ThemeToggle component
   - T004: Wire toggle into App.tsx navigation
   - T005: Add dark styles to all pages

5. /speckit.implement → Execute approved tasks
```

---

## Reference: Which Doc Answers What

| Question | Consult |
|----------|---------|
| What tables exist? | `docs/05-database.md` |
| What endpoints exist? | `docs/04-backend.md` |
| What pages/components exist? | `docs/03-frontend.md` |
| What are the security rules? | `docs/06-security-authentication-authorization.md` |
| What integrations are used? | `docs/07-integrations.md` |
| How is the system architected? | `docs/01-current-architecture.md` |
| What modules are implemented? | `docs/02-functional-map.md` |
| What risks should I know? | `docs/12-risks.md` |
| What technical debt exists? | `docs/13-technical-debt.md` |
| What are the critical flows? | `docs/10-critical-flows.md` |
| What gaps need attention? | `docs/14-gaps-and-recommendations.md` |
| High-level project overview? | `docs/15-executive-summary.md` |

---

## What NOT to Do

- ❌ Don't run `/speckit.implement` without completing spec → plan → tasks first
- ❌ Don't skip reading the relevant docs for context before starting
- ❌ Don't change the database without a plan (40+ unversioned migrations exist)
- ❌ Don't modify the constitution without documenting and approving the change
- ❌ Don't create new features during documentation/audit phases
- ❌ Don't present inferences as facts — mark uncertainty explicitly

---

## Prohibited Actions (Always)

1. ❌ Execute `/speckit.implement` without authorization
2. ❌ Rewrite modules
3. ❌ Change architecture
4. ❌ Change stack (React, FastAPI, Supabase, TON)
5. ❌ Install dependencies without justification and approval
6. ❌ Change database schema without migration
7. ❌ Create parallel authentication systems
8. ❌ Delete code or rename folders without approval
9. ❌ Hide errors
10. ❌ Invent features — document only what exists

---

## Spec Lifecycle Summary

```
Ask for change
    │
    ▼
/speckit.specify   ─── Creates spec.md
    │
    ▼
/speckit.clarify   ─── Resolves ambiguities (optional, recommended)
    │
    ▼
/speckit.plan      ─── Creates plan.md (must pass constitution)
    │
    ▼
/speckit.checklist ─── Quality checklist (optional)
    │
    ▼
/speckit.tasks     ─── Creates tasks.md
    │
    ▼
/speckit.analyze   ─── Consistency check (optional, before implement)
    │
    ▼
/speckit.implement ─── Executes tasks (AUTHORIZATION REQUIRED)
    │
    ▼
/speckit.converge  ─── Assess & append remaining work
```

---

**Remember**: The constitution, docs, and specs are your **single source of truth**. Every future change starts with a spec.
