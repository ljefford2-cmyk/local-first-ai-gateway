# SPEC 8 — Managed Build Workflow

**Status:** Draft v0.3
**Applies to:** DRNT Gateway development process
**Related:** SPEC-MAP.md, STATUS.md, THREAT-MODEL.md

**Changes from v0.2:** Relaxed the 3-file Elevated trigger from absolute to presumptive, with a tests/docs/non-structural exception (§9.0); defined Routine-cycle critic default and its exceptions (§9.1); tightened behavior-verifiable criteria to require a stated observation method (§7.1); added commit-subject tagging requirement for accepted-with-exception overrides (§11.4).

**Changes from v0.1:** Softened tool-selection framing (§2); refined Critic role wording (§4.4); introduced Routine vs Elevated two-tier path (§7, §8, new §9.0); defined accepted-with-exception state (§8, §9.4); added acceptance-criterion type taxonomy (§10); cleaned up SPEC-MAP integration language (§11.1).

---

## 1. Purpose and Doctrine

DRNT's thesis is that governed, graduated-trust, fail-closed, human-decides systems produce better outcomes than unconstrained autonomy. The runtime already demonstrates this thesis against the agents DRNT governs.

This spec applies the same thesis to DRNT's own development. The agents building DRNT are themselves governed by graduated trust, review gates, human approval at consequential moments, and a durable audit trail. The build workflow is not exempt from the doctrine it exists to prove.

Operating principle: **no rule applies to a DRNT runtime agent that does not also apply, in spirit, to a DRNT build agent.** Rules that would be absurd for a runtime agent are absurd for a build agent.

## 2. Scope and Non-Goals

**In scope.** The human-to-agent workflow for implementing changes to the DRNT Gateway codebase: planning, building, verifying, critiquing, and reporting. Approval gates applied to development activity. Role contracts and authority boundaries. Artifact templates for plans and build-run reports.

**Out of scope.** Runtime agent governance (covered by Specs 1–7 and capabilities.json). Infrastructure provisioning. Hardware and network architecture. Vendor procurement. Current incumbent tools are documented in §5 for workflow accuracy; substitution policy is governed by §12.

**Non-goals.**
- Replacing human judgment on product direction.
- Automating acceptance of agent output without human decision at consequential gates.
- Introducing CI/CD complexity beyond what this workflow requires.
- Mandating specific vendors.

## 3. Dogfooding Statement

DRNT claims that governed, graduated-trust, fail-closed, human-decides systems produce better outcomes than unconstrained autonomy. Spec 8 applies that same doctrine to DRNT's own development workflow.

If the workflow defined here cannot be lived with during DRNT's own construction, the thesis is weaker than claimed. If it can, the project becomes its own evidence. Every plan approved, every build run reported, every verifier block, and every human override is a data point for or against the doctrine.

This is not decorative. It is the primary reason Spec 8 exists as a spec rather than a development-process memo.

## 4. Role Definitions

Five roles. Each has defined inputs, outputs, authority, and prohibitions. A single tool or model may fill multiple roles in Stage 1, but the role boundaries remain distinct.

### 4.1 Planner

**Purpose.** Translate a human objective into an approved, bounded implementation plan.

**Inputs.** Human-stated objective. Current repo state. Relevant prior specs, STATUS claims, and SCENARIOS.

**Outputs.** A Plan Artifact (§7) submitted for human approval.

**Authority.** May read the full repo. May propose scope, risks, and acceptance criteria. May recommend role assignments for the build.

**Prohibitions.** May not write to the codebase. May not mark a plan approved. May not begin implementation under any circumstance.

**Current incumbent.** Claude.ai planning conversation (the operator working with a model to scope the work).

### 4.2 Builder

**Purpose.** Implement the approved plan, and only the approved plan.

**Inputs.** An approved Plan Artifact. Repo access scoped to the files in the plan.

**Outputs.** Code changes. A proposed Build-Run Report (§8) draft covering what was actually done.

**Authority.** May modify files listed in the plan. May add tests. May run the test suite. May ask the human clarifying questions when the plan is ambiguous.

**Prohibitions.** May not expand scope beyond the plan. May not modify files not listed in the plan without stopping and requesting an amended plan. May not mark its own work complete. May not self-certify correctness.

**Current incumbent.** Claude Code, running against the DRNT repo.

### 4.3 Verifier

**Purpose.** Determine whether the builder's output satisfies the plan's acceptance criteria, and block acceptance if it does not.

**Inputs.** The approved plan. The builder's changes and draft report. The test suite. Existing verification helpers (`_assert_valid_envelope`, `MockAuditClient`, `drnt_audit_verify.py`).

**Outputs.** A verifier finding: **pass**, **fail**, or **uncertain**, with evidence. Fail and uncertain findings include specific reasons and the acceptance criteria unmet.

**Authority.** **May block acceptance.** The reporter may not mark a build successful over a verifier fail. Only the human may override a verifier fail, and only via an explicit, audited override (§9.4).

**Prohibitions.** May not modify code. May not re-run the builder. May not soften findings to reduce friction. "Looks reasonable" is not a pass.

**Current incumbent.** Initially filled by the human plus the test suite. A dedicated verifier agent is the first planned addition.

### 4.4 Critic

**Purpose.** Adversarial review of the plan, the implementation, or both. Surfaces blind spots, model-specific biases, and architectural drift that the builder and verifier will not catch because they share the builder's framing.

**Inputs.** The plan and/or the builder's changes. The six design principles. Spec 8 itself, including reserved terms (§6).

**Outputs.** A critique identifying concerns, ranked by severity. The critic does not propose fixes — that is the planner's role on the next cycle.

**Authority.** May raise concerns that the human must acknowledge before acceptance. May not block unilaterally.

**Discipline.** Must prioritize faults, risks, omissions, and drift. Must not dilute critique with reassurance or generic praise. May note when a concern raised in a prior review has been addressed — that is signal, not praise.

**Prohibitions.** May not summarize for its own sake. May not rewrite the work. May not moderate its findings because the builder would be offended.

**Current incumbent.** The multi-model adversarial panel — ChatGPT, Gemini, Grok, with Claude rotated out when Claude is the builder. Panel composition is chosen per review to cover bias surface area.

### 4.5 Reporter

**Purpose.** Produce the plain-English Build-Run Report (§8) that the human uses to make the acceptance decision.

**Inputs.** The plan, the builder's changes, the verifier's finding, the critic's review, the test run output.

**Outputs.** A single Build-Run Report in the defined format.

**Authority.** May summarize and translate. May identify the decisions the human needs to make.

**Prohibitions.** May not reinterpret a verifier fail as a pass. May not omit critic concerns. May not soften uncertainty into confidence. If the verifier said uncertain, the report says uncertain.

**Current incumbent.** The Claude.ai session used for planning closes the loop as reporter, producing the report for operator review.

## 5. Current Incumbent Tools

| Role      | Current tool                                              |
|-----------|-----------------------------------------------------------|
| Planner   | Claude.ai (operator + model in planning conversation)     |
| Builder   | Claude Code, DRNT repo                                    |
| Verifier  | Human + pytest suite (dedicated agent is next addition)   |
| Critic    | Multi-model adversarial panel (ChatGPT, Gemini, Grok)     |
| Reporter  | Claude.ai session closing the loop                        |

Substitutions are governed by §12.

## 6. Reserved Terms and Forbidden Collisions

Spec 8 introduces new vocabulary to the project. To prevent contamination of runtime doctrine, the following rules are non-negotiable.

**6.1 Reserved runtime terms — MUST NOT be redefined in build-workflow contexts:**
- `pipeline` — reserved for `PipelineEvaluator` and `_run_pipeline()`; means permission-checking chain.
- `dispatch` — reserved for `dispatch_local`, `dispatch_cloud`, `dispatch_multi`; means routing a job to execution.
- `capability` — reserved for the central runtime entity in capabilities.json.
- `promotion` / `demotion` — reserved for WAL state transitions governed by PromotionMonitor and DemotionEngine.
- `governing` / `auxiliary` — reserved for capability types.

**6.2 Build-workflow terms — introduced by this spec:**
- `planner`, `builder`, `verifier`, `critic`, `reporter` — build-workflow roles only.
- `plan artifact` — pre-implementation work packet (§7).
- `build-run report` — post-implementation report (§8).
- `build approval gate` — human decision point on development activity (§9).
- `accepted-with-exception` — acceptance state after verifier-fail override (§9.4).

**6.3 Enforcement.** Any future amendment to Spec 8, or any plan artifact, that repurposes a §6.1 term for build-workflow meaning fails review on that basis alone. This rule binds future sessions of this spec's own authors.

## 7. Plan Artifact

Every build cycle begins with a plan. No implementation begins without human approval. The plan exists in one of two forms — Routine or Elevated — chosen by §9.0 triage.

### 7.1 Elevated Plan

For consequential work (§9.0). Full markdown artifact, committed to `docs/plans/`:

```
# Plan: <short title>

## Objective
One to three sentences. What outcome does this plan achieve?

## Scope
Bulleted list of what this plan covers.

## Out of Scope
Bulleted list of what this plan explicitly does not cover.

## Files Likely Affected
Path list, with one-line rationale per path. New files marked [NEW].

## Risks
Named risks with severity (low/medium/high). For each, how the plan mitigates it.

## Acceptance Criteria
Numbered list. Each criterion tagged as one of:
  - [test-verifiable] — a specific test confirms it
  - [file-state-verifiable] — a specific file state confirms it
  - [behavior-verifiable] — a specific observable behavior confirms it;
    the criterion MUST state the observation method (command to run,
    endpoint to call, UI action to perform). A behavior-verifiable
    criterion without a stated observation method fails review as
    disguised human judgment.
  - [human-judgment-required] — only human assessment confirms it

At least one criterion MUST be mechanically checkable (test-, file-state-,
or behavior-verifiable). A plan whose criteria are all human-judgment-required
fails review.

## Human Approval Required
YES / NO, with reason. YES is mandatory for §9.2 categories.

## Role Assignments
Planner: <tool or session>
Builder: <tool or session>
Verifier: <tool or session>
Critic: <panel composition>
Reporter: <tool or session>

## Reserved-Terms Check
Has this plan been reviewed for §6.1 reserved-term collisions? YES / NO.
```

Elevated plans are committed to `docs/plans/` with the commit that begins their implementation. Rejected or superseded plans are retained as history.

### 7.2 Routine Plan

For non-consequential work (§9.0). Inline in the commit message or PR description, not a separate file:

```
Plan:
- Objective: <one sentence>
- Files: <path list>
- Acceptance criteria:
  1. [test-verifiable] <criterion>
  2. [file-state-verifiable] <criterion>
```

Same criterion-tag rule applies: at least one must be mechanically checkable.

## 8. Build-Run Report

Every build cycle ends with a report. No build is marked complete without one. The form depends on the path (§9.0).

### 8.1 Elevated Report

Committed to `docs/reports/` alongside the commit that closes the build:

```
# Build-Run Report: <plan title>

## Summary (Plain English)
One paragraph. What changed for the user or the system. No jargon.
If a non-coder cannot understand this paragraph, the report fails.

## Files Changed
Path list with one-line description of the change per path.

## Build Status
PASS / FAIL / UNCERTAIN. Evidence: command run, output excerpt.

## Test Status
PASS / FAIL / UNCERTAIN. Counts: <passed> passed, <failed> failed, <skipped> skipped.
If any previously-passing test now fails, it is listed here by name.

## Verifier Finding
PASS / FAIL / UNCERTAIN. Reasoning. Acceptance criteria status (per §7 criterion,
with criterion type preserved).

## Critic Review
Concerns raised, ranked by severity. Concerns the reporter believes are addressed
are listed with why. Concerns not yet addressed are listed as OPEN.

## Unresolved Risks
Anything the verifier or critic flagged as uncertain. Anything the builder
flagged for human judgment.

## Decisions Required from Human
Numbered list. Each decision has a default (what happens if the human says
"proceed") and an alternative. If no decisions are required, state so explicitly.

## Acceptance
[ ] Accepted.
[ ] Accepted-with-exception (verifier FAIL override). Reason: <required>.
[ ] Rejected.
[ ] Need more information.

(Human marks this section. The reporter does not mark it.)
```

### 8.2 Routine Report

Inline in the commit message, not a separate file:

```
Report:
- Summary: <one-sentence plain-English description>
- Tests: <N> passed, <F> failed, <S> skipped
- Verifier: PASS | FAIL (override: <reason>) | UNCERTAIN
- Open concerns: <list, or "none">
```

If the routine report would include a verifier FAIL or UNCERTAIN, the cycle automatically escalates to the Elevated path (§9.0) before acceptance.

## 9. Approval Gates and Human Decision Points

### 9.0 Routine vs Elevated Triage

Every build cycle is triaged before planning. A cycle is **Elevated** if any of the following are true:

1. The work falls into any §9.2 category (architecture, dependencies, schema, security, egress, audit, capability policy).
2. More than 3 files will be touched — **unless** all touched files are tests, documentation, or non-structural changes, in which case the planner may keep the cycle Routine. The planner states the exception reason in the plan.
3. The cycle causes any test regression, even transient.
4. More than one acceptance criterion is tagged `human-judgment-required`.
5. The builder or verifier escalates the cycle mid-flight (e.g., scope grew, uncertainty emerged).

Otherwise the cycle is **Routine**. Routine cycles use inline plan and inline report (§7.2, §8.2); elevated cycles use full artifacts (§7.1, §8.1).

Triage is performed by the planner and confirmed by the human at plan approval. If in doubt, choose Elevated — over-ceremony on a single cycle is cheaper than under-governance on a consequential one.

### 9.1 Routine default: `on_accept`

For Routine cycles, the gate is `on_accept`: the builder runs, the verifier verifies, the critic critiques (see rule below), the reporter produces the inline report, and the human accepts or rejects the package as a whole.

**Critic default for Routine cycles.** Critic review defaults to **skipped** for Routine cycles. It is **expected** when:
- The planner flags drift risk in the plan.
- The builder expands scope mid-flight.
- The verifier returns UNCERTAIN.
- The acceptance criteria include any `human-judgment-required` criterion.

When critic review is skipped, the inline report notes "Critic: skipped (routine)". When expected, it is performed before the reporter produces the inline report.

Rationale: accepting every intermediate step creates friction without governance benefit. The runtime doesn't make humans approve every egress packet; Spec 8 doesn't make the operator approve every line.

### 9.2 Elevated: `pre_action` for consequential changes

The following categories require `pre_action` review — human approval of the **plan** before the builder begins:

1. Architecture changes (new services, new major modules, changes to the orchestrator/worker/audit trust boundary).
2. Dependency additions or version bumps.
3. Schema changes (capabilities.json structure, audit event envelope, persistence tables).
4. Security posture changes (sandbox constraints, egress policies, seccomp profiles, capability-drop lists).
5. Egress changes (new allowed endpoints, new providers, new network policies).
6. Audit changes (event types, chain format, emit-durable semantics).
7. Capability policy changes (WAL levels, review gates, promotion criteria, cost gates).

These categories match, by intent, the items DRNT itself treats as most consequential at runtime. The build workflow mirrors the runtime's own sense of what is grave.

### 9.3 Delivery hold: `pre_delivery` for verifier-uncertain results

When the verifier returns uncertain (not fail, not pass), the gate escalates to `pre_delivery`: the work is held, the report is produced (as Elevated per §9.0 clause 5), and the human decides before the build is marked complete or rolled back. This prevents uncertain results from silently becoming accepted results.

### 9.4 Override and the accepted-with-exception state

A human may override a verifier FAIL. The override requires:
- Explicit statement in the Acceptance section of the report (checkbox: "Accepted-with-exception").
- A reason recorded in the commit message.
- A note in STATUS.md if the override affects a tracked claim.

**Acceptance after override is `accepted-with-exception`, not `accepted`.** This distinction is non-negotiable. `accepted` and `accepted-with-exception` are different states. The commit message, the report, and any STATUS entry all use `accepted-with-exception` when the verifier finding was FAIL and the human chose to proceed anyway.

Rationale: without this distinction, a verifier FAIL plus human override becomes indistinguishable from a clean PASS in git history. The workflow would launder failures into successes over time.

Overrides are allowed. Silent overrides are not. Laundered overrides are forbidden.

## 10. Verifier Role: Authority, Inputs, Outputs

The verifier is the one genuinely new role this spec introduces. It needs sharper definition than the others.

**10.1 Inputs (required).**
- The approved plan, with acceptance criteria and criterion tags.
- The builder's diff.
- The builder's draft report.
- The current state of the test suite before and after the build.

**10.2 Inputs (available if needed).**
- STATUS.md, to check whether modified code is Implemented, Partial, or Aspirational.
- SPEC-MAP.md, to check cross-spec impact.
- The existing verification helpers.

**10.3 Criterion verification rules.** Each acceptance criterion carries a type tag (§7). The verifier must check criteria according to their type:

- `test-verifiable` — verified by running the named or relevant test(s). The finding cites the test result.
- `file-state-verifiable` — verified by inspecting the specified file(s). The finding cites the file state observed.
- `behavior-verifiable` — verified by executing the specified behavior and observing the result. The finding cites what was observed.
- `human-judgment-required` — the verifier marks this as DEFERRED-TO-HUMAN, not PASS or FAIL. The human resolves it at acceptance.

The verifier must not treat a `human-judgment-required` criterion as PASS based on its own assessment. That would be scope creep into judgment the verifier is not authorized to exercise.

**10.4 Outputs.** A structured finding:

```
Verifier Finding: PASS | FAIL | UNCERTAIN

Acceptance Criteria:
  1. [<type>] <criterion>: MET | UNMET | UNCERTAIN | DEFERRED-TO-HUMAN
     Evidence: <test result, file state, behavior observed, or "deferred">
  2. ...

Tests:
  Before: <N> passing
  After:  <M> passing, <F> failing, <S> skipped
  Regressions: <list, or "none">

Additional Observations:
  <anything the verifier noticed that is not a criterion violation but
   may warrant reporter or human attention>
```

**10.5 Blocking authority.** A FAIL finding blocks acceptance. The reporter's report must surface the FAIL and its reasoning. The only path forward is human override (§9.4, producing `accepted-with-exception`) or a new build cycle that addresses the failure.

**10.6 Boundaries.** The verifier does not rewrite the builder's work. It does not suggest fixes. It does not soften findings. If the criterion is unmet, the finding is UNMET, even if the gap is small.

**10.7 Stage 1 filler.** Until a dedicated verifier agent exists, the human plus the test suite fill this role. The structured finding format is followed regardless of who produces it.

## 11. Integration with SPEC-MAP, STATUS, and Test Structure

Spec 8 is a working document in the project's living spec set, not a parallel universe. Specific integration points:

**11.1 SPEC-MAP.md.** Add Spec 8 as a row:
- **Primary artifacts:** `docs/SPEC-8-MANAGED-BUILD-WORKFLOW.md`, `docs/plans/`, `docs/reports/`
- **Runtime modules impacted:** none directly (process spec)
- **Config files:** none
- **Test files:** none
- **Notes:** Governs development workflow; see spec document.

**11.2 STATUS.md.** When a build affects a tracked claim, the build-run report notes the claim ID and the status change (e.g., "Claim 6.7: Partial → Implemented"). STATUS.md is updated in the same commit, not separately. When acceptance is `accepted-with-exception`, the STATUS note reflects that state.

**11.3 Test structure.** No changes to test taxonomy. The existing phase-aligned structure (`test_phaseNx.py`, `test_spec7x_*.py`) continues. New specs continue the pattern.

**11.4 Commit conventions.** Existing Conventional Commits format is retained. Elevated builds add:
```
Plan: docs/plans/<plan-file>.md
Report: docs/reports/<report-file>.md
Verifier: PASS | FAIL (override: <reason>) | UNCERTAIN (override: <reason>)
Acceptance: accepted | accepted-with-exception | rejected
```

Routine builds include the inline plan and inline report in the commit message body (§7.2, §8.2). Acceptance state is still declared explicitly.

**Override visibility rule.** When acceptance is `accepted-with-exception` (verifier FAIL override per §9.4), the commit subject line MUST include the `[accepted-with-exception]` tag. Body placement alone is insufficient. This applies to both Routine and Elevated cycles. Rationale: overrides must be greppable at the subject level for later audit of override frequency and pattern.

**11.5 Audit parallel.** The runtime audit log is the primary durable record of governed action. The git history + `docs/plans/` + `docs/reports/` together form the build workflow's equivalent. This is a parallel, not a unification — the runtime audit chain does not include build activity, and should not.

## 12. Amendment and Tool Substitution Policy

**12.1 Amendments.** Spec 8 is amended by the same workflow it governs: a plan is written, approved, built, verified, critiqued, and reported. Amendments to §6.1 (reserved terms) require elevated (`pre_action`) approval. All spec amendments are Elevated by default.

**12.2 Tool substitution.** §5 names incumbent tools. Substituting a tool for a role requires:
1. An Elevated plan artifact naming the current and proposed tool, with rationale.
2. Explicit human approval (`pre_action`).
3. A transition build run where the new tool fills the role under observation, with the previous tool or the human as a fallback verifier.
4. An amendment to §5.

Tool substitutions are not casual. The workflow depends on each role being filled by something that actually discharges the role's authority and prohibitions.

**12.3 Role addition.** Adding a sixth role (e.g., a dedicated security reviewer) requires a full amendment, not a tool substitution. Role definitions are load-bearing; new roles ripple through §4, §5, §9, and §10.

**12.4 Version history.** Amendments bump the spec version (v0.1 → v0.2, etc.). Prior versions are retained in git history. STATUS.md-style claim tracking is not applied to Spec 8 itself; its status is its current text.

---

**End of SPEC 8 v0.3 draft.**
