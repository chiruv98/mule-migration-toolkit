#!/usr/bin/env python3
"""
Stage 1 — Parse Mule 4 XML → ir.json
No LLM. Pure XML analysis.

Usage:
    python tool/parse.py --app order-api [--project-root .]
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ── Mule 4 XML namespaces ──────────────────────────────────────────────────────
NS = {
    'mule':   'http://www.mulesoft.org/schema/mule/core',
    'http':   'http://www.mulesoft.org/schema/mule/http',
    'db':     'http://www.mulesoft.org/schema/mule/db',
    'jms':    'http://www.mulesoft.org/schema/mule/jms',
    'ee':     'http://www.mulesoft.org/schema/mule/ee/core',
    'batch':  'http://www.mulesoft.org/schema/mule/batch',
    'apikit': 'http://www.mulesoft.org/schema/mule/mule-apikit',
}

# ── Mule error type → (JavaExceptionClass, httpStatus, errorCode) ─────────────
ERROR_MAP = {
    'DB:CONNECTIVITY':         ('DatabaseUnavailableException', 503, 'DB_UNAVAILABLE'),
    'DB:QUERY_EXECUTION':      ('QueryExecutionException',      500, 'QUERY_FAILED'),
    'VALIDATION:INVALID_INPUT':('InvalidInputException',        400, 'INVALID_INPUT'),
    'HTTP:NOT_FOUND':          ('ResourceNotFoundException',    404, 'NOT_FOUND'),
    'HTTP:TIMEOUT':            ('ServiceTimeoutException',      504, 'TIMEOUT'),
    'HTTP:CONNECTIVITY':       ('ServiceUnavailableException',  503, 'SERVICE_UNAVAILABLE'),
    'MULE:COMPOSITE_ROUTING':  ('CompositeRoutingException',    207, 'PARTIAL_SUCCESS'),
    'BATCH:JOB_EXECUTION':     ('BatchJobException',            500, 'BATCH_FAILED'),
    'INVOICE:INVALID_RECORD':  ('InvalidRecordException',       422, 'INVALID_RECORD'),
    'ANY':                     ('Exception',                    500, 'INTERNAL_ERROR'),
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def tag(prefix, local):
    return f'{{{NS[prefix]}}}{local}'

def get_text(elem):
    return (elem.text or '').strip() if elem is not None else ''

def snake_to_camel(s):
    parts = s.split('_')
    return parts[0] + ''.join(p.title() for p in parts[1:])

def snake_to_pascal(s):
    return ''.join(p.title() for p in s.split('_'))

def infer_java_type(col):
    n = col.lower()
    if n == 'id' or n.endswith('_id'):           return 'Long'
    if any(n.endswith(x) for x in ('_amount','_price','_cost','_rate','_total','_fee')): return 'BigDecimal'
    if any(n.endswith(x) for x in ('_at','_date','_time','_timestamp')): return 'LocalDateTime'
    if any(n.endswith(x) for x in ('_count','_qty','_quantity','_num','_size')): return 'Integer'
    if n in ('active','enabled','deleted','published','verified','flagged'): return 'Boolean'
    return 'String'

def classify_dataweave(expr):
    body = re.sub(r'%dw\s+\d+\.\d+', '', expr)
    body = re.sub(r'output\s+\S+/\S+', '', body)
    body = re.sub(r'---', '', body).strip()

    if re.match(r'^payload(\s+as\s+\S+)?\s*$', body):
        return 'format-only'

    if any(k in expr for k in ('payload.*', '.payload map', 'payload.*.payload', 'failures map')):
        return 'aggregation'

    structural = [' map ', '\nmap ', 'map (', 'map(', ' filter ', 'filter(',
                  'reduce(', 'sum(', 'groupBy', 'distinctBy', 'mergeWith',
                  'flatten', 'orderBy', 'pluck', 'splitBy', 'zip(']
    if any(k in expr for k in structural):
        return 'structural'

    return 'field-mapping'

def output_type_from_expr(expr):
    m = re.search(r'output\s+(\S+)', expr)
    return m.group(1) if m else 'application/java'

def extract_sql_meta(sql):
    tables, cols = [], []
    m = re.search(r'SELECT\s+(.*?)\s+FROM\s+(\w+)', sql, re.IGNORECASE | re.DOTALL)
    if m:
        raw = m.group(1).strip()
        tables.append(m.group(2).strip())
        if raw != '*':
            cols = [c.strip().split('.')[-1] for c in raw.split(',')]
    for pat in (r'INSERT\s+INTO\s+(\w+)', r'UPDATE\s+(\w+)\s+SET', r'FROM\s+(\w+)'):
        for mm in re.finditer(pat, sql, re.IGNORECASE):
            t = mm.group(1).strip()
            if t not in tables:
                tables.append(t)
    return tables, cols

def parse_flow_trigger_from_name(name):
    m = re.match(r'^(get|post|put|delete|patch):[\\\/ ](.*?)(?::.*)?$', name, re.IGNORECASE)
    if m:
        method = m.group(1).upper()
        path = '/' + m.group(2).replace('\\', '/').lstrip('/')
        return method, path
    return None, None


# ── DataWeave extractor (recursive — handles nested scopes) ───────────────────

def extract_dw(elem, flow_name, counter):
    results = []

    def _scan(node, location_hint=''):
        for child in node:
            local = child.tag.split('}')[-1] if '}' in child.tag else child.tag

            if child.tag == tag('ee', 'transform'):
                for sp in child.iter(tag('ee', 'set-payload')):
                    expr = get_text(sp)
                    if expr and '%dw' in expr:
                        counter[0] += 1
                        cls = classify_dataweave(expr)
                        results.append({
                            'id': f'DW{counter[0]}',
                            'flow': flow_name,
                            'location': f'{location_hint}ee:transform.set-payload',
                            'input_type': 'application/java',
                            'output_type': output_type_from_expr(expr),
                            'classification': cls,
                            'send_to_llm': cls != 'format-only',
                            'raw_expression': expr,
                        })
                for sv in child.iter(tag('ee', 'set-variable')):
                    expr = get_text(sv)
                    if expr and '%dw' in expr:
                        counter[0] += 1
                        cls = classify_dataweave(expr)
                        var_name = sv.get('variableName', 'unknown')
                        results.append({
                            'id': f'DW{counter[0]}',
                            'flow': flow_name,
                            'location': f'{location_hint}ee:transform.set-variable({var_name})',
                            'input_type': 'application/json',
                            'output_type': output_type_from_expr(expr),
                            'classification': cls,
                            'send_to_llm': cls != 'format-only',
                            'raw_expression': expr,
                        })

            elif child.tag == tag('jms', 'publish'):
                dest = child.get('destination', 'unknown')
                for body in child.iter(tag('jms', 'body')):
                    expr = get_text(body)
                    if expr and '%dw' in expr:
                        counter[0] += 1
                        cls = classify_dataweave(expr)
                        results.append({
                            'id': f'DW{counter[0]}',
                            'flow': flow_name,
                            'location': f'{location_hint}jms:publish({dest}).body',
                            'input_type': 'application/java',
                            'output_type': 'application/json',
                            'classification': cls,
                            'send_to_llm': cls != 'format-only',
                            'raw_expression': expr,
                        })

            elif child.tag == tag('http', 'request'):
                for body in child.iter(tag('http', 'body')):
                    expr = get_text(body)
                    if expr and '%dw' in expr:
                        counter[0] += 1
                        cls = classify_dataweave(expr)
                        results.append({
                            'id': f'DW{counter[0]}',
                            'flow': flow_name,
                            'location': f'{location_hint}http:request.body',
                            'input_type': 'application/java',
                            'output_type': 'application/json',
                            'classification': cls,
                            'send_to_llm': cls != 'format-only',
                            'raw_expression': expr,
                        })

            # Recurse into scopes: scatter-gather routes, batch steps, error-handlers
            _scan(child, location_hint)

    _scan(elem)
    return results


# ── Error handler extractor ───────────────────────────────────────────────────

def extract_error_handlers(elem, flow_name):
    handlers = []
    for eh in elem.iter(tag('mule', 'error-handler')):
        for child in eh:
            local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if local not in ('on-error-continue', 'on-error-propagate'):
                continue
            err_type = child.get('type', 'ANY')
            strategy = 'continue' if local == 'on-error-continue' else 'propagate'
            info = ERROR_MAP.get(err_type, ('ApiException', 500, 'API_ERROR'))
            handlers.append({
                'flow': flow_name,
                'mule_type': err_type,
                'strategy': strategy,
                'exception_class': info[0],
                'http_status': info[1],
                'error_code': info[2],
            })
    return handlers


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_mule_xml(xml_path):
    tree = ET.parse(xml_path)
    root = tree.getroot()

    flows = []
    all_dw = []
    all_errors = []
    tables = {}          # table → [columns]
    jms_destinations = []
    constructs = set()
    dw_counter = [0]
    has_batch = False
    has_scatter_gather = False

    # APIKit router presence
    has_apikit = root.find(f'.//{tag("apikit","router")}') is not None
    if has_apikit:
        constructs.add('apikit:router')

    # ── Top-level flows ────────────────────────────────────────────────────────
    for flow_elem in root.findall(tag('mule', 'flow')):
        flow_name = flow_elem.get('name', '')
        trigger = None

        # HTTP listener (explicit)
        listener = flow_elem.find(tag('http', 'listener'))
        if listener is not None:
            constructs.add('http:listener')
            path_attr = listener.get('path', '/')
            method_attr = listener.get('method', 'GET').upper()
            trigger = {'type': 'http:listener', 'method': method_attr, 'path': path_attr}

        # APIKit-style flow name carries method + path
        if trigger is None:
            method, path = parse_flow_trigger_from_name(flow_name)
            if method:
                constructs.add('http:listener')
                trigger = {'type': 'http:listener', 'method': method, 'path': path}

        # Batch job
        batch_job = flow_elem.find(f'.//{tag("batch","job")}')
        if batch_job is not None:
            has_batch = True
            constructs.add('batch:job')
            if trigger is None:
                trigger = {'type': 'batch:job', 'name': batch_job.get('jobName', flow_name)}

        # Scatter-gather
        if flow_elem.find(f'.//{tag("mule","scatter-gather")}') is not None:
            has_scatter_gather = True
            constructs.add('scatter-gather')

        # DB operations + SQL metadata
        for db_op in ('select', 'insert', 'update', 'delete'):
            for db_elem in flow_elem.iter(tag('db', db_op)):
                constructs.add(f'db:{db_op}')
                sql_elem = db_elem.find(tag('db', 'sql'))
                sql = get_text(sql_elem)
                t_list, cols = extract_sql_meta(sql)
                for t in t_list:
                    if t not in tables or (cols and not tables[t]):
                        tables[t] = cols

        # JMS destinations
        for jms_pub in flow_elem.iter(tag('jms', 'publish')):
            constructs.add('jms:publish')
            dest = jms_pub.get('destination', 'unknown')
            if dest not in jms_destinations:
                jms_destinations.append(dest)

        # HTTP client
        if flow_elem.find(f'.//{tag("http","request")}') is not None:
            constructs.add('http:request')

        # Collect DataWeave and error handlers
        dw_exprs = extract_dw(flow_elem, flow_name, dw_counter)
        all_dw.extend(dw_exprs)
        errors = extract_error_handlers(flow_elem, flow_name)
        all_errors.extend(errors)

        # Processor list (readable summary)
        processors = [c.split('}')[-1] for c in constructs if ':' in c]

        if trigger:
            flows.append({'name': flow_name, 'trigger': trigger, 'processors': list(constructs)})

    # Global error handler
    for geh in root.findall(tag('mule', 'error-handler')):
        errors = extract_error_handlers(geh, 'global')
        all_errors.extend(errors)

    # ── Build entities ─────────────────────────────────────────────────────────
    entities = []
    for tbl, cols in tables.items():
        singular = tbl.rstrip('s') if tbl.endswith('s') and len(tbl) > 3 else tbl
        entity_name = snake_to_pascal(singular)
        fields = [
            {
                'column': col,
                'field': snake_to_camel(col),
                'java_type': infer_java_type(col),
                'pk': col == 'id',
                'generated': col == 'id',
                'nullable': col not in ('id',),
            }
            for col in cols
        ]
        entities.append({'name': entity_name, 'table': tbl, 'fields': fields})

    # ── Complexity tier ────────────────────────────────────────────────────────
    score = len(flows)
    score += 5 if has_batch else 0
    score += 3 if has_scatter_gather else 0
    score += len(set(c.split(':')[0] for c in constructs if ':' in c))
    score += 2 * sum(1 for dw in all_dw if dw['classification'] in ('structural', 'aggregation'))
    tier = 'T1' if score <= 6 else ('T2' if score <= 14 else 'T3')

    # ── LLM call estimate ──────────────────────────────────────────────────────
    llm_needed = [dw for dw in all_dw if dw['send_to_llm']]
    field_maps  = [dw for dw in llm_needed if dw['classification'] == 'field-mapping']
    structural  = [dw for dw in llm_needed if dw['classification'] in ('structural', 'aggregation')]
    llm_calls = max(1, -(-len(field_maps) // 3)) + len(structural)  # ceil(n/3) + structural

    return {
        'app': '',
        'complexity_tier': tier,
        'constructs': sorted(constructs),
        'flows': flows,
        'entities': entities,
        'connectors': {
            'db':           {'present': bool(tables),           'tables': list(tables.keys())},
            'jms':          {'present': bool(jms_destinations), 'destinations': jms_destinations},
            'http_client':  {'present': 'http:request' in constructs},
            'batch':        {'present': has_batch},
            'scatter_gather': {'present': has_scatter_gather},
        },
        'dataweave': all_dw,
        'error_handlers': all_errors,
        'has_apikit': has_apikit,
        'llm_calls_estimate': llm_calls,
    }


def main():
    parser = argparse.ArgumentParser(description='Parse Mule 4 XML → ir.json')
    parser.add_argument('--app',          required=True, help='App folder name under mule-apps/')
    parser.add_argument('--project-root', default='.',   help='Project root directory')
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    mule_dir     = project_root / 'mule-apps' / args.app / 'src' / 'main' / 'mule'
    out_dir      = project_root / 'output' / args.app
    out_dir.mkdir(parents=True, exist_ok=True)

    xml_files = list(mule_dir.glob('*.xml'))
    if not xml_files:
        print(f'ERROR: No XML files found in {mule_dir}', file=sys.stderr)
        sys.exit(1)

    # Merge results from all XML files in the app
    merged = {'app': args.app, 'constructs': set(), 'flows': [], 'entities': [],
              'dataweave': [], 'error_handlers': [],
              'connectors': {'db': {'present': False, 'tables': []},
                             'jms': {'present': False, 'destinations': []},
                             'http_client': {'present': False},
                             'batch': {'present': False},
                             'scatter_gather': {'present': False}},
              'has_apikit': False}

    for xml_file in xml_files:
        data = parse_mule_xml(xml_file)
        merged['constructs'].update(data['constructs'])
        merged['flows'].extend(data['flows'])
        merged['entities'].extend(data['entities'])
        merged['dataweave'].extend(data['dataweave'])
        merged['error_handlers'].extend(data['error_handlers'])
        merged['has_apikit'] = merged['has_apikit'] or data['has_apikit']
        for k in ('db', 'jms', 'http_client', 'batch', 'scatter_gather'):
            if data['connectors'][k]['present']:
                merged['connectors'][k]['present'] = True
            if k in ('db', 'jms'):
                existing = merged['connectors'][k].get('tables') or merged['connectors'][k].get('destinations') or []
                new_items = data['connectors'][k].get('tables') or data['connectors'][k].get('destinations') or []
                key = 'tables' if k == 'db' else 'destinations'
                merged['connectors'][k][key] = list(dict.fromkeys(existing + new_items))

    merged['constructs'] = sorted(merged['constructs'])

    # Recompute tier and llm_calls on merged data
    score  = len(merged['flows'])
    score += 5 if merged['connectors']['batch']['present'] else 0
    score += 3 if merged['connectors']['scatter_gather']['present'] else 0
    score += len(set(c.split(':')[0] for c in merged['constructs'] if ':' in c))
    score += 2 * sum(1 for dw in merged['dataweave'] if dw['classification'] in ('structural','aggregation'))
    merged['complexity_tier'] = 'T1' if score <= 6 else ('T2' if score <= 14 else 'T3')

    llm_dw      = [dw for dw in merged['dataweave'] if dw['send_to_llm']]
    field_maps  = [dw for dw in llm_dw if dw['classification'] == 'field-mapping']
    structural  = [dw for dw in llm_dw if dw['classification'] in ('structural','aggregation')]
    merged['llm_calls_estimate'] = max(1, -(-len(field_maps) // 3)) + len(structural)

    out_path = out_dir / 'ir.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(merged, f, indent=2)

    # ── Summary ────────────────────────────────────────────────────────────────
    format_only = sum(1 for dw in merged['dataweave'] if not dw['send_to_llm'])
    llm_needed  = sum(1 for dw in merged['dataweave'] if dw['send_to_llm'])
    print(f'\n── Analysis: {args.app} {"─" * (40 - len(args.app))}')
    print(f'  Flows:      {len(merged["flows"])}')
    print(f'  Constructs: {", ".join(merged["constructs"])}')
    print(f'  DataWeave:  {len(merged["dataweave"])} expressions '
          f'({format_only} format-only → skip LLM, {llm_needed} need translation)')
    print(f'  LLM calls:  ~{merged["llm_calls_estimate"]} (after batching)')
    print(f'  Tier:       {merged["complexity_tier"]}')
    print(f'  IR saved:   {out_path}')
    print('─' * 50)


if __name__ == '__main__':
    main()
