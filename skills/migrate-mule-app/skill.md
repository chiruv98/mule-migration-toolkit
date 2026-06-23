# Skill: migrate-mule-app

Migrate a Mule 4 application to Spring Boot using the Mule Migration Toolkit.

**Stages 0, 1, 1.5, 2, 4 — Python scripts, zero LLM tokens.**
**Stage 3A/3B/3C — Claude translates only: DataWeave fragments, unknown connector stubs, custom DW functions.**

## Usage

```
/migrate-mule-app <mule-app-folder-name>
```

## Prerequisites (once per project)

```bash
pip install -r {skill-root}/tool/requirements.txt
```

`migration-strategy.yaml` must exist at the project root.
Copy from `{skill-root}/migration-strategy.template.yaml` if starting fresh.

---

## STEP 0 — Confirm contract

Read `{project-root}/migration-strategy.yaml`.
Print:
```
Contract: async={async.style}, db={data.db_access}, jms={messaging.jms_client}, error={error_handling.strategy}
```
Stop and ask the user to copy the template if the file does not exist.

---

## STEP 1 — Parse: Mule XML → Semantic IR (no LLM)

```bash
python "{skill-root}/tool/parse.py" --app {app} --project-root "{project-root}"
```

Enhanced parser handles:
- APIKit router + explicit `http:listener` flows
- Sub-flows and flow-ref chains (within-file resolution)
- Batch jobs, scatter-gather, schedulers
- DataWeave classifier: format-only | field-mapping | structural | aggregation
  (P0: `fun`, `import`, `using`, `type`, `dw::` module refs → structural)
- Third-party connector detection (Salesforce, SAP, S3, Kafka, etc.)
- HTTP client connection config extraction
- Cross-file import warnings

Output: `{project-root}/output/{app}/ir.json`
**Tokens: 0**

---

## STEP 1a — RAML/OAS Reader: API spec → type contract (no LLM)

```bash
python "{skill-root}/tool/raml_reader.py" --app {app} --project-root "{project-root}"
```

If a RAML 1.0 or OAS 3.0 spec is found in the app's `src/main/resources/api/` folder:
- Extracts all type definitions → Java record/enum descriptors
- Extracts all endpoints with authoritative request/response types
- Overrides flow-name inference for controller generation

Output: `{project-root}/output/{app}/raml_ir.json`
**Tokens: 0**

---

## STEP 1b — Enrich: IR + RAML + connector registry (no LLM)

```bash
python "{skill-root}/tool/enrich.py" --app {app} --project-root "{project-root}" --skill-root "{skill-root}"
```

Merges all static analysis:
- RAML types → `ir.raml_types` (generates Java records in Step 2)
- RAML endpoints → flow contracts (authoritative typing for controller/service)
- Unknown connectors matched against `connector_registry.yaml` → Spring pattern + LLM instruction
- DataWeave AST scan: custom function extraction, module import detection
- Flow-ref graph resolution
- Updated LLM call estimate broken into Stage 3A / 3B / 3C

Output: enriched `{project-root}/output/{app}/ir.json`
**Tokens: 0**

---

## STEP 2 — Generate: scaffold from enriched IR (no LLM)

```bash
python "{skill-root}/tool/generate.py" --app {app} --project-root "{project-root}" --skill-root "{skill-root}"
```

Generates:
- `pom.xml` — all deps including extra connector deps from registry
- `application.yml` — DB, JMS, resilience config
- `@SpringBootApplication` main class
- `@RestController` — endpoints typed from RAML contract if available
- `@Service` — method stubs with `// TODO Stage 3` markers
- `@Mapper` (MapStruct) — field-mapping stubs
- `@Entity` + `@Repository` — from SQL metadata in IR
- **DTO records** — Java 21 records from RAML types (`src/.../dto/`)
- **Enum classes** — from RAML enum types
- `JmsConfig.java` — if JMS present
- `ApiException.java` + subclasses + `GlobalExceptionHandler.java`

**Tokens: 0**

---

## STEP 3 — Translate: LLM fills in what static tools cannot (scoped calls only)

Read `{project-root}/output/{app}/ir.json`.

**Inject this constraint block before every LLM call:**
```
Contract constraints — follow exactly, no exceptions:
- JSON: Jackson / @JsonProperty
- Object mapping: MapStruct @Mapper
- Array operations: Java Streams (no Reactor types)
- Async: virtual threads + CompletableFuture (NO Mono, NO Flux)
- Java 21: records, switch expressions, text blocks where natural
- Errors: throw ApiException subclass — never try/catch in controllers
```

### Stage 3A — DataWeave translation
For each entry in `ir.dataweave` where `send_to_llm: true`:
- **Batch rule**: group ≤3 `field-mapping` with the same output type per call
- **Structural/aggregation**: one call each
- Each call receives: constraint block + IR fragment (id, flow, classification, input_type, output_type, raw_expression)
- NEVER send the raw Mule XML — only IR fragments
- If `has_custom_functions: true`, also include `ast.custom_functions` in the call
- Fill in `// TODO Stage 3` stub in the generated file

Print: `  ✓ {DW_id} [{classification}] → {file}:{method}`

### Stage 3B — Unknown connector stubs
For each unique namespace in `ir.unknown_connectors` where `send_to_llm: true`:
- Use the `llm_instruction` from the registry entry as the call prompt
- Call receives: constraint block + registry instruction + all connector usages for that namespace
- Generate a `{Namespace}Service.java` in `src/.../service/`
- Add `extra_pom_deps` from IR to pom.xml if not already present

Print: `  ✓ {namespace} connector → {NamespaceService}.java`

### Stage 3C — Custom DataWeave function translation
For each entry in `ir.dataweave` where `has_custom_functions: true`:
- Call receives: constraint block + full expression + `ast.custom_functions` list + `ast.module_imports`
- LLM must return both: the translated expression AND a `// UTIL:` labelled Java utility method
- Place utility methods in `src/.../util/{ClassPrefix}DwUtils.java`

Print: `  ✓ {DW_id} [custom-fn] → {DwUtils}.java + {file}:{method}`

---

## STEP 4 — Verify consistency (no LLM)

```bash
python "{skill-root}/tool/verify.py" --app {app} --project-root "{project-root}"
```

Checks: no Mono/Flux, no @Retryable, no try/catch in controllers, @RestControllerAdvice present,
JmsTemplate.convertAndSend (if JMS), @Mapper present (if field-mapping DW), no unfilled TODOs,
no `var` keyword.

**Tokens: 0**

---

## Pipeline token efficiency

| Stage | LLM calls | What's sent |
|---|---|---|
| 0 Contract | 0 | — |
| 1 Parse | 0 | — |
| 1a RAML | 0 | — |
| 1b Enrich | 0 | — |
| 2 Generate | 0 | — |
| 3A DataWeave | ~ceil(n/3) + structural | IR fragments only |
| 3B Connectors | 1 per namespace | Registry instruction + usages |
| 3C Custom DW | 1 per custom-fn expr | Expression + AST |
| 4 Verify | 0 | — |

---

## Hard Rules

- NEVER send raw Mule XML to LLM — only IR fragments from ir.json
- NEVER invent patterns not in migration-strategy.yaml
- NEVER skip verify.py
- NEVER use `var` in generated Java
- NEVER generate `try/catch` in controllers
- ALL connector stubs must implement the interface from `spring_pattern` in connector_registry.yaml
