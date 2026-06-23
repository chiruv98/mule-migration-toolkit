#!/usr/bin/env python3
"""
Stage 4 — Verify generated Spring Boot project consistency.
No LLM. Grep-based checks against the Migration Strategy Contract.

Usage:
    python tool/verify.py --app order-api [--project-root .]
"""

import argparse
import json
import re
import sys
from pathlib import Path


def grep_files(directory, pattern, file_glob='*.java'):
    """Return list of (file, line_no, line) matching pattern."""
    matches = []
    for java_file in Path(directory).rglob(file_glob):
        for i, line in enumerate(java_file.read_text(encoding='utf-8').splitlines(), 1):
            if re.search(pattern, line):
                matches.append((str(java_file.relative_to(directory)), i, line.strip()))
    return matches


def check(label, matches, expect_zero=True):
    """Print a check result. Returns True if check passed."""
    if expect_zero:
        passed = len(matches) == 0
        status = 'PASS' if passed else f'FAIL ({len(matches)} violation{"s" if len(matches) > 1 else ""})'
    else:
        passed = len(matches) > 0
        status = 'PASS' if passed else 'FAIL (not found)'

    print(f'  {label:<35} {status}')
    if not passed and expect_zero:
        for f, ln, line in matches[:3]:
            print(f'    → {f}:{ln}  {line}')
    return passed


def main():
    parser = argparse.ArgumentParser(description='Verify generated Spring Boot project')
    parser.add_argument('--app',          required=True)
    parser.add_argument('--project-root', default='.')
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    out_dir      = project_root / 'output' / args.app
    java_dir     = out_dir / 'src' / 'main' / 'java'
    ir_path      = out_dir / 'ir.json'

    if not java_dir.exists():
        print(f'ERROR: {java_dir} does not exist — run generate.py first', file=sys.stderr)
        sys.exit(1)

    ir = {}
    if ir_path.exists():
        ir = json.loads(ir_path.read_text(encoding='utf-8'))

    print(f'\n── Consistency Check: {args.app} {"─" * (35 - len(args.app))}')

    results = []

    # 1. No reactive types
    results.append(check(
        'No reactive types (Mono/Flux/Publisher)',
        grep_files(java_dir, r'(Mono<|Flux<|Publisher<)')
    ))

    # 2. No Spring Retry
    results.append(check(
        'No @Retryable (contract: resilience4j)',
        grep_files(java_dir, r'@Retryable')
    ))

    # 3. No try/catch in controllers
    ctrl_dir = java_dir / 'controller' if (java_dir / 'controller').exists() else java_dir
    results.append(check(
        'No try/catch in controllers',
        grep_files(ctrl_dir, r'^\s*try\s*\{')
    ))

    # 4. Error handler pattern — @RestControllerAdvice present
    results.append(check(
        '@RestControllerAdvice present',
        grep_files(java_dir, r'@RestControllerAdvice'),
        expect_zero=False
    ))

    # 5. JMS — if JMS present in IR, verify JmsTemplate.convertAndSend
    if ir.get('connectors', {}).get('jms', {}).get('present'):
        results.append(check(
            'JmsTemplate.convertAndSend used',
            grep_files(java_dir, r'convertAndSend'),
            expect_zero=False
        ))
        results.append(check(
            'No raw MessageProducer (must use JmsTemplate)',
            grep_files(java_dir, r'MessageProducer')
        ))

    # 6. MapStruct — if field-mapping DataWeave present, verify Mapper exists
    has_field_mapping = any(
        dw['classification'] == 'field-mapping' and dw['send_to_llm']
        for dw in ir.get('dataweave', [])
    )
    if has_field_mapping:
        results.append(check(
            'MapStruct @Mapper interface present',
            grep_files(java_dir, r'@Mapper\(componentModel'),
            expect_zero=False
        ))

    # 7. No TODO stubs left unfilled in service/batch
    svc_dir = java_dir
    todo_matches = grep_files(svc_dir, r'//\s*TODO Stage 3')
    if todo_matches:
        print(f'  {"Unfilled TODO stubs":<35} WARN ({len(todo_matches)} remaining)')
        for f, ln, line in todo_matches[:5]:
            print(f'    → {f}:{ln}  {line}')
    else:
        print(f'  {"Unfilled TODO stubs":<35} PASS')

    # 8. Verify no var keyword (contract: readability)
    results.append(check(
        'No var keyword in generated code',
        grep_files(java_dir, r'\bvar\s+\w+\s*=')
    ))

    # ── Summary ────────────────────────────────────────────────────────────────
    passed = sum(results)
    total  = len(results)
    print('─' * 55)
    print(f'  Result: {passed}/{total} checks passed')
    if passed < total:
        print('  ✗ Fix violations before demo.')
        sys.exit(1)
    else:
        print('  ✓ All checks passed.')
    print()


if __name__ == '__main__':
    main()
