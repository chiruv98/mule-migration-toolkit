#!/usr/bin/env python3
"""
Stage 1.5 — Enrich ir.json with RAML types, connector stubs, and DW AST analysis.
No LLM.

What it does:
  - Merges raml_ir.json into ir.json (types → entities, endpoints override flows)
  - Applies connector_registry.yaml to unknown connectors → adds stub_type + llm_instruction
  - Lightweight DataWeave AST scan: extracts custom function names, module imports
  - Resolves flow-ref chains across the merged flow list
  - Recalculates llm_calls_estimate
  - Updates llm_calls_estimate to include Stage 3B (unknown connectors) + 3C (custom DW fns)

Usage:
    python enrich.py --app order-api [--project-root .] [--skill-root .]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


# ── DataWeave lightweight AST scanner (P2) ─────────────────────────────────────────

def scan_dw_ast(expr):
    custom_functions = []
    module_imports   = []
    type_aliases     = []

    for m in re.finditer(r'\bfun\s+(\w+)\s*\(([^)]*)\)', expr):
        fn_name  = m.group(1)
        fn_args  = [a.strip().split(':')[0].strip() for a in m.group(2).split(',') if a.strip()]
        fn_start = m.end()
        fn_match = re.search(r'\bfun\s+\w+|^---', expr[fn_start:], re.MULTILINE)
        fn_body  = expr[fn_start: fn_start + (fn_match.start() if fn_match else 200)].strip()
        custom_functions.append({
            'name':       fn_name,
            'args':       fn_args,
            'body_snippet': fn_body[:120] + ('...' if len(fn_body) > 120 else ''),
        })

    for m in re.finditer(r'import\s+(.*?)\s+from\s+([\w:]+)', expr):
        what   = m.group(1).strip()
        module = m.group(2).strip()
        module_imports.append({'what': what, 'module': module})

    for m in re.finditer(r'^\s*type\s+(\w+)\s*=\s*(.+?)$', expr, re.MULTILINE):
        type_aliases.append({
            'name':       m.group(1),
            'definition': m.group(2).strip()[:80],
        })

    score = (
        len(custom_functions) * 3 +
        len(module_imports) * 2 +
        len(type_aliases) * 1 +
        expr.count(' map ') +
        expr.count('reduce(') +
        expr.count('groupBy') +
        expr.count('flatMap') +
        expr.count('mapObject')
    )

    return {
        'custom_functions': custom_functions,
        'module_imports':   module_imports,
        'type_aliases':     type_aliases,
        'complexity_score': score,
    }


# ── Flow-ref graph resolution (P0/P1) ───────────────────────────────────────────

def resolve_flow_graph(flows, flow_graph):
    all_names   = {f['name'] for f in flows}
    refs        = flow_graph.get('refs', {})
    resolved    = set()
    unresolved  = set()
    chains      = {}

    for flow_name, ref_list in refs.items():
        chain = []
        for ref in ref_list:
            if ref in all_names:
                resolved.add(ref)
                chain.append(ref)
            else:
                unresolved.add(ref)
        if chain:
            chains[flow_name] = chain

    return {
        'all_flow_names': sorted(all_names),
        'resolved':       sorted(resolved),
        'unresolved':     sorted(unresolved),
        'chains':         chains,
    }


# ── Connector registry application (P1) ──────────────────────────────────────────

def apply_connector_registry(unknown_connectors, registry_path):
    if not registry_path.exists():
        return unknown_connectors, []

    registry_raw = yaml.safe_load(registry_path.read_text(encoding='utf-8'))
    registry     = {c['namespace']: c for c in registry_raw.get('connectors', [])}

    extra_deps = []
    enriched   = []

    for conn in unknown_connectors:
        ns = conn.get('namespace', '')
        reg = registry.get(ns)
        if reg:
            conn = {
                **conn,
                'display':         reg.get('display', ns),
                'spring_pattern':  reg.get('spring_pattern', 'manual implementation required'),
                'stub_type':       reg.get('stub_type', 'rest_client'),
                'llm_instruction': reg.get('llm_instruction', ''),
                'registry_match':  True,
            }
            for dep in reg.get('dependencies', []):
                if dep not in extra_deps:
                    extra_deps.append(dep)
        else:
            conn = {
                **conn,
                'display':         ns,
                'spring_pattern':  'manual implementation required — no registry match',
                'stub_type':       'rest_client',
                'llm_instruction': f'Convert this {ns} connector operation to a Spring @Service. '
                                   f'Document what it does in a // TODO: comment and create a stub.',
                'registry_match':  False,
            }
        enriched.append(conn)

    return enriched, extra_deps


# ── RAML IR merge (P1) ─────────────────────────────────────────────────────────────

def merge_raml_into_ir(ir, raml_ir):
    if not raml_ir or not raml_ir.get('format'):
        ir['raml'] = {'present': False}
        return ir

    ir['raml'] = {
        'present':   True,
        'format':    raml_ir['format'],
        'title':     raml_ir['title'],
        'version':   raml_ir['version'],
        'base_path': raml_ir['base_path'],
    }

    raml_types = []
    for t in raml_ir.get('types', []):
        if t['kind'] not in ('object', 'enum'):
            continue
        raml_types.append({
            **t,
            'source': 'raml',
        })

    ir['raml_types'] = raml_types

    raml_endpoints = raml_ir.get('endpoints', [])
    if raml_endpoints:
        def normalize_path(p):
            return re.sub(r'\{[^}]+\}', '{param}', p.lower())

        raml_ep_map = {}
        for ep in raml_endpoints:
            key = (ep['method'].upper(), normalize_path(ep['path']))
            raml_ep_map[key] = ep

        for flow in ir.get('flows', []):
            trigger = flow.get('trigger') or {}
            if trigger.get('type') == 'http:listener':
                key = (trigger.get('method', 'GET').upper(),
                       normalize_path(trigger.get('path', '/')))
                raml_ep = raml_ep_map.get(key)
                if raml_ep:
                    flow['raml_contract'] = {
                        'operation_id':  raml_ep['operation_id'],
                        'request_type':  raml_ep.get('request_type'),
                        'response_type': raml_ep.get('response_type'),
                        'query_params':  raml_ep.get('query_params', []),
                        'description':   raml_ep.get('description', ''),
                    }

    return ir


# ── LLM call estimate recalculation ──────────────────────────────────────────────

def recalculate_llm_estimate(ir):
    llm_dw       = [dw for dw in ir.get('dataweave', []) if dw.get('send_to_llm')]
    field_maps   = [dw for dw in llm_dw if dw['classification'] == 'field-mapping']
    structural   = [dw for dw in llm_dw if dw['classification'] in ('structural', 'aggregation')]
    unknown_ns   = len(set(c['namespace'] for c in ir.get('unknown_connectors', [])))
    custom_dw    = len([dw for dw in ir.get('dataweave', []) if dw.get('has_custom_functions')])

    dw_calls     = max(1, -(-len(field_maps) // 3)) + len(structural)
    total        = dw_calls + unknown_ns + custom_dw

    ir['llm_calls_estimate'] = total
    ir['llm_breakdown'] = {
        'stage_3a_dataweave':          dw_calls,
        'stage_3b_unknown_connectors': unknown_ns,
        'stage_3c_custom_dw_fns':      custom_dw,
        'total':                       total,
    }
    return ir


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Enrich ir.json with RAML, connector stubs, DW AST')
    parser.add_argument('--app',          required=True)
    parser.add_argument('--project-root', default='.')
    parser.add_argument('--skill-root',   default='.')
    args = parser.parse_args()

    project_root  = Path(args.project_root).resolve()
    skill_root    = Path(args.skill_root).resolve()
    out_dir       = project_root / 'output' / args.app
    ir_path       = out_dir / 'ir.json'
    raml_ir_path  = out_dir / 'raml_ir.json'
    registry_path = skill_root / 'tool' / 'connector_registry.yaml'

    if not ir_path.exists():
        print(f'ERROR: {ir_path} not found — run parse.py first', file=sys.stderr)
        sys.exit(1)

    ir = json.loads(ir_path.read_text(encoding='utf-8'))

    raml_ir = None
    if raml_ir_path.exists():
        raml_ir = json.loads(raml_ir_path.read_text(encoding='utf-8'))
    ir = merge_raml_into_ir(ir, raml_ir)

    enriched_connectors, extra_deps = apply_connector_registry(
        ir.get('unknown_connectors', []), registry_path
    )
    ir['unknown_connectors'] = enriched_connectors
    ir['extra_pom_dependencies'] = extra_deps

    for dw in ir.get('dataweave', []):
        if dw.get('has_custom_functions') and dw.get('raw_expression'):
            ast_meta = scan_dw_ast(dw['raw_expression'])
            dw['ast'] = ast_meta
            if ast_meta['complexity_score'] > 5 and dw['classification'] == 'field-mapping':
                dw['classification'] = 'structural'

    flow_graph_resolved = resolve_flow_graph(
        ir.get('flows', []), ir.get('flow_graph', {})
    )
    ir['flow_graph']['resolved_graph'] = flow_graph_resolved

    ir = recalculate_llm_estimate(ir)
    ir['enriched'] = True

    ir_path.write_text(json.dumps(ir, indent=2), encoding='utf-8')

    breakdown = ir.get('llm_breakdown', {})
    raml      = ir.get('raml', {})
    unknown   = ir.get('unknown_connectors', [])
    custom_dw = sum(1 for dw in ir.get('dataweave', []) if dw.get('has_custom_functions'))

    print(f'\n── Enrichment: {args.app} {"─" * (40 - len(args.app))}')
    if raml.get('present'):
        print(f'  RAML/OAS:     {raml["format"]} — {len(ir.get("raml_types", []))} types merged')
        enriched_flows = sum(1 for f in ir.get('flows', []) if f.get('raml_contract'))
        print(f'  Flow contract: {enriched_flows} flows have authoritative RAML endpoint typing')
    else:
        print('  RAML/OAS:     not found — using flow-name inference')

    if unknown:
        matched   = sum(1 for c in unknown if c.get('registry_match'))
        unmatched = len(unknown) - matched
        print(f'  Connectors:   {matched} registry-matched, {unmatched} manual stubs needed')

    if custom_dw:
        print(f'  DW AST:       {custom_dw} expressions with custom functions scanned')

    if ir['flow_graph'].get('resolved_graph', {}).get('unresolved'):
        for u in ir['flow_graph']['resolved_graph']['unresolved'][:3]:
            print(f'  ⚠ Unresolved flow-ref: {u}')

    if extra_deps:
        print(f'  Extra deps:   {len(extra_deps)} connector dependencies added to IR')

    print(f'  LLM estimate: ~{ir["llm_calls_estimate"]} calls total '
          f'(3A: {breakdown.get("stage_3a_dataweave",0)} DW, '
          f'3B: {breakdown.get("stage_3b_unknown_connectors",0)} connectors, '
          f'3C: {breakdown.get("stage_3c_custom_dw_fns",0)} custom DW)')
    print(f'  Saved:        {ir_path}')
    print('─' * 50)


if __name__ == '__main__':
    main()
