# Skill: migrate-mule-app

Migrate a Mule 4 application to Spring Boot using the Mule Migration Toolkit.

**Stages 1, 2, 4 — Python scripts, zero LLM tokens.**
**Stage 3 — Claude translates only DataWeave expressions using scoped IR fragments.**

## Usage

```
/migrate-mule-app <mule-app-folder-name>
```

## Prerequisites (once per project)

```bash
pip install -r {skill-root}/tool/requirements.txt
```

`migration-strategy.yaml` must exist at the project root.
Copy it from `{skill-root}/migration-strategy.template.yaml` if starting fresh.

---

## STEP 0 — Confirm contract

Read `{project-root}/migration-strategy.yaml`.
Print:
```
Contract: async={async.style}, db={data.db_access}, jms={messaging.jms_client}, error={error_handling.strategy}
```
If the file does not exist, stop and tell the user to copy `{skill-root}/migration-strategy.template.yaml` to the project root.

---

## STEP 1 — Parse: Mule XML → Semantic IR (no LLM)

```bash
python "{skill-root}/tool/parse.py" --app {app} --project-root "{project-root}"
```

Produces `{project-root}/output/{app}/ir.json`. Wait for summary before continuing.

**Tokens used: 0**

---

## STEP 2 — Generate: scaffold from IR + contract (no LLM)

```bash
python "{skill-root}/tool/generate.py" --app {app} --project-root "{project-root}" --skill-root "{skill-root}"
```

Renders Jinja2 templates → Spring Boot scaffold files. Service and controller methods have `// TODO Stage 3` stubs.

**Tokens used: 0**

---

## STEP 3 — Translate: DataWeave → Java (LLM, scoped only)

Read `{project-root}/output/{app}/ir.json`. Identify all entries where `send_to_llm: true`.

**Prepend to every LLM call:**
```
Contract constraints — follow exactly, no exceptions:
- JSON: Jackson / @JsonProperty
- Object mapping: MapStruct @Mapper
- Array operations: Java Streams (no Reactor types)
- Async: virtual threads + CompletableFuture (NO Mono, NO Flux)
- Java 21: records, switch expressions, text blocks
- Errors: throw ApiException subclass — never try/catch in controllers
```

**Batching rule:** Group up to 3 `field-mapping` expressions with the same output type into one call. Each `structural` or `aggregation` expression gets its own call.

**Each call receives:** constraint block + one IR fragment (raw DataWeave expression, input/output types, flow context). Never the full Mule XML.

After each call, fill in the `// TODO Stage 3` stub in the generated Java file.

Print: `  ✓ {DW_id} → {file}:{method}`

---

## STEP 4 — Verify consistency (no LLM)

```bash
python "{skill-root}/tool/verify.py" --app {app} --project-root "{project-root}"
```

All checks must pass. If any FAIL, fix and re-run.

**Tokens used: 0**

---

## Consistency guarantee

Every run reads `migration-strategy.yaml` from the project root before any generation.
The same pattern elected (e.g. `jms_client: spring-jms`) appears identically in every migrated app in the project.

---

## Hard Rules

- NEVER send raw Mule XML to LLM — only IR fragments from ir.json
- NEVER invent patterns not in migration-strategy.yaml
- NEVER skip verify.py
- NEVER use `var` in generated Java
- NEVER generate `try/catch` in controllers
