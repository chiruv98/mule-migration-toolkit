#!/usr/bin/env python3
"""
Stage 1 — Parse Mule 4 XML → ir.json
No LLM. Pure XML analysis.

Enhancements (P0/P1/P2):
  - P0: flow-ref resolution within file, better DW classifier (fun/import/using/type)
  - P0: http:request-connection extraction for real client configs
  - P0: cross-file import detection + warnings
  - P1: sub-flow parsing, error handler inheritance model
  - P2: DataWeave custom function detection for enrich.py

Usage:
    python parse.py --app order-api [--project-root .]
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {
    'mule':   'http://www.mulesoft.org/schema/mule/core',
    'http':   'http://www.mulesoft.org/schema/mule/http',
    'db':     'http://www.mulesoft.org/schema/mule/db',
    'jms':    'http://www.mulesoft.org/schema/mule/jms',
    'ee':     'http://www.mulesoft.org/schema/mule/ee/core',
    'batch':  'http://www.mulesoft.org/schema/mule/batch',
    'apikit': 'http://www.mulesoft.org/schema/mule/mule-apikit',
    'vm':     'http://www.mulesoft.org/schema/mule/vm',
    'sftp':   'http://www.mulesoft.org/schema/mule/sftp',
    'file':   'http://www.mulesoft.org/schema/mule/file',
    'scheduler': 'http://www.mulesoft.org/schema/mule/scheduler',
    'oauth':  'http://www.mulesoft.org/schema/mule/oauth',
}

ERROR_MAP = {
    'DB:CONNECTIVITY':          ('DatabaseUnavailableException', 503, 'DB_UNAVAILABLE'),
    'DB:QUERY_EXECUTION':       ('QueryExecutionException',      500, 'QUERY_FAILED'),
    'VALIDATION:INVALID_INPUT': ('InvalidInputException',        400, 'INVALID_INPUT'),
    'HTTP:NOT_FOUND':           ('ResourceNotFoundException',    404, 'NOT_FOUND'),
    'HTTP:TIMEOUT':             ('ServiceTimeoutException',      504, 'TIMEOUT'),
    'HTTP:CONNECTIVITY':        ('ServiceUnavailableException',  503, 'SERVICE_UNAVAILABLE'),
    'HTTP:UNAUTHORIZED':        ('UnauthorizedException',        401, 'UNAUTHORIZED'),
    'HTTP:FORBIDDEN':           ('ForbiddenException',           403, 'FORBIDDEN'),
    'MULE:COMPOSITE_ROUTING':   ('CompositeRoutingException',    207, 'PARTIAL_SUCCESS'),
    'MULE:EXPRESSION':          ('ExpressionException',          400, 'EXPRESSION_ERROR'),
    'MULE:ROUTING':             ('RoutingException',             500, 'ROUTING_ERROR'),
    'MULE:SECURITY':            ('SecurityException',            401, 'SECURITY_ERROR'),
    'BATCH:JOB_EXECUTION':      ('BatchJobException',            500, 'BATCH_FAILED'),
    'INVOICE:INVALID_RECORD':   ('InvalidRecordException',       422, 'INVALID_RECORD'),
    'ANY':                      ('Exception',                    500, 'INTERNAL_ERROR'),
}

# Known third-party connector namespaces — enrich.py maps these to Spring stubs
KNOWN_CUSTOM_NAMESPACES = {
    'salesforce', 'sap', 'netsuite', 'servicenow', 'workday',
    'twilio', 'stripe', 'sendgrid', 'kafka', 'rabbitmq',
    'ftp', 'smtp', 'pop3', 'imap', 'ldap', 'redis',
    's3', 'sqs', 'dynamodb', 'sns', 'kinesis',
    'mongodb', 'cassandra', 'couchbase',
    'soap', 'wsc', 'web-service-consumer',
    'objectstore', 'cloudhub',
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def tag(prefix, local):
    return f'{{{NS[prefix]}}}{local}'

def get_ns_prefix(full_tag):
    """Extract namespace prefix from a Clark-notation tag."""
    if '}' not in full_tag:
        return None
    ns_uri = full_tag[1:full_tag.index('}')]
    for prefix, uri in NS.items():
        if uri == ns_uri:
            return prefix
    # Unknown namespace — extract last segment of URI as hint
    return ns_uri.rstrip('/').split('/')[-1]

def get_text(elem):
    return (elem.text or '').strip() if elem is not None else ''

def snake_to_camel(s):
    parts = s.split('_')
    return parts[0] + ''.join(p.title() for p in parts[1:])

def snake_to_pascal(s):
    return ''.join(p.title() for p in s.split('_'))

def kebab_to_pascal(s):
    return ''.join(p.title() for p in re.split(r'[-_]', s))

def infer_java_type(col):
    n = col.lower()
    if n == 'id' or n.endswith('_id'):
        return 'Long'
    if any(n.endswith(x) for x in ('_amount', '_price', '_cost', '_rate', '_total', '_fee', '_balance')):
        return 'BigDecimal'
    if any(n.endswith(x) for x in ('_at', '_date', '_time', '_timestamp', '_on')):
        return 'LocalDateTime'
    if any(n.endswith(x) for x in ('_count', '_qty', '_quantity', '_num', '_size', '_age', '_year')):
        return 'Integer'
    if n in ('active', 'enabled', 'deleted', 'published', 'verified', 'flagged', 'archived', 'is_active'):
        return 'Boolean'
    return 'String'


# ── DataWeave classifier (P0 enhanced) ────────────────────────────────────────

def classify_dataweave(expr):
    """
    Classify DataWeave expression complexity.
    P0: adds fun/import/using/type detection → structural (not field-mapping).
    """
    body = re.sub(r'%dw\s+\d+\.\d+', '', expr)
    body_no_output = re.sub(r'output\s+\S+/\S+', '', body)
    body_clean = re.sub(r'---', '', body_no_output).strip()

    # Format-only: bare payload passthrough
    if re.match(r'^payload(\s+as\s+\S+)?\s*$', body_clean):
        return 'format-only'

    # P0: custom functions, module imports, type aliases → always structural
    if re.search(r'\bfun\s+\w+\s*\(', expr):
        return 'structural'
    if re.search(r'^import\s+', expr, re.MULTILINE):
        return 'structural'
    if re.search(r'\busing\s*\(', expr):
        return 'structural'
    if re.search(r'^\s*type\s+\w+\s*=', expr, re.MULTILINE):
        return 'structural'
    # dw:: module references (e.g. dw::core::Strings)
    if re.search(r'\bdw::', expr):
        return 'structural'

    # Aggregation patterns (scatter-gather result merging)
    if any(k in expr for k in ('payload.*', '.payload map', 'payload.*.payload', 'failures map', 'inboundProperties')):
        return 'aggregation'

    # Structural: collection operations
    structural_keywords = [
        ' map ', '\nmap ', 'map (', 'map(',
        ' filter ', 'filter(',
        'reduce(', 'sum(', 'avg(',
        'groupBy', 'distinctBy', 'mergeWith',
        'flatten', 'orderBy', 'pluck', 'splitBy', 'zip(',
        'mapObject', 'filterObject', 'keysOf', 'valuesOf',
        'sizeOf', 'isEmpty', 'isBlank',
        ' match ', ' matches ', 'scan(',
        'read(', 'write(',
        'update ', ' replace ',
    ]
    if any(k in expr for k in structural_keywords):
        return 'structural'

    return 'field-mapping'


def has_custom_dw_functions(expr):
    """P2: detect whether this DW expression defines or calls custom functions."""
    return bool(re.search(r'\bfun\s+\w+\s*\(', expr)) or \
           bool(re.search(r'^import\s+', expr, re.MULTILINE)) or \
           bool(re.search(r'\bdw::', expr))


def output_type_from_expr(expr):
    m = re.search(r'output\s+(\S+)', expr)
    return m.group(1) if m else 'application/java'


# ── SQL metadata ──────────────────────────────────────────────────────────────

def extract_sql_meta(sql):
    tables, cols = [], []
    m = re.search(r'SELECT\s+(.*?)\s+FROM\s+(\w+)', sql, re.IGNORECASE | re.DOTALL)
    if m:
        raw = m.group(1).strip()
        tables.append(m.group(2).strip())
        if raw != '*':
            cols = [c.strip().split('.')[-1].split(' ')[-1] for c in raw.split(',')]
    for pat in (r'INSERT\s+INTO\s+(\w+)', r'UPDATE\s+(\w+)\s+SET', r'FROM\s+(\w+)'):
        for mm in re.finditer(pat, sql, re.IGNORECASE):
            t = mm.group(1).strip()
            if t.upper() not in ('SELECT', 'WHERE', 'JOIN', 'ON') and t not in tables:
                tables.append(t)
    return tables, cols


# ── Flow trigger inference ─────────────────────────────────────────────────────

def parse_flow_trigger_from_name(name):
    """APIKit-style: 'get:\\orders:{config}' → (GET, /orders)"""
    m = re.match(r'^(get|post|put|delete|patch|head|options):[\\\\\\\/{](.*?)(?::.*)?$', name, re.IGNORECASE)
    if m:
        method = m.group(1).upper()
        raw_path = m.group(2).replace('\\', '/').lstrip('/')
        path = '/' + re.sub(r'\\/', '/', raw_path)
        return method, path
    return None, None


# ── HTTP config extractor (P0) ────────────────────────────────────────────────

def extract_http_configs(root):
    """
    Extract http:request-config / http:request-connection for outbound HTTP calls.
    Returns dict: config_name → {host, port, protocol, base_path}
    """
    configs = {}
    for rc in root.iter(tag('http', 'request-config')):
        name = rc.get('name', '')
        conn = rc.find(tag('http', 'request-connection'))
        if conn is not None:
            configs[name] = {
                'host':      conn.get('host', 'localhost'),
                'port':      int(conn.get('port', 80)),
                'protocol':  conn.get('protocol', 'HTTP').upper(),
                'base_path': conn.get('basePath', '/'),
            }
        else:
            configs[name] = {
                'host':      rc.get('host', 'localhost'),
                'port':      int(rc.get('port', 80)),
                'protocol':  rc.get('protocol', 'HTTP').upper(),
                'base_path': rc.get('basePath', '/'),
            }
    listener_configs = {}
    for lc in root.iter(tag('http', 'listener-config')):
        name = lc.get('name', '')
        conn = lc.find(tag('http', 'listener-connection'))
        if conn is not None:
            listener_configs[name] = {
                'host': conn.get('host', '0.0.0.0'),
                'port': int(conn.get('port', 8081)),
                'protocol': conn.get('protocol', 'HTTP').upper(),
            }
        else:
            listener_configs[name] = {
                'host': lc.get('host', '0.0.0.0'),
                'port': int(lc.get('port', 8081)),
                'protocol': lc.get('protocol', 'HTTP').upper(),
            }
    return configs, listener_configs


# ── Unknown connector detector (P1) ──────────────────────────────────────────

def detect_unknown_connectors(elem, flow_name):
    """
    Detect third-party / custom connector usages not in NS dict.
    Returns list of {namespace, element, attributes, flow, send_to_llm}.
    """
    found = []
    for child in elem.iter():
        if '}' not in child.tag:
            continue
        ns_uri = child.tag[1:child.tag.index('}')]
        local  = child.tag[child.tag.index('}')+1:]
        known = any(ns_uri == v for v in NS.values())
        if not known:
            connector_name = ns_uri.rstrip('/').split('/')[-1].split('.')[-1]
            if connector_name in KNOWN_CUSTOM_NAMESPACES or connector_name not in ('xsi', 'xs', 'doc', 'xml'):
                attrs = {k: v for k, v in child.attrib.items()
                         if not k.startswith('{') and k not in ('doc:name',)}
                found.append({
                    'namespace':  connector_name,
                    'element':    local,
                    'attributes': attrs,
                    'flow':       flow_name,
                    'send_to_llm': True,
                })
    return found


# ── DataWeave extractor ────────────────────────────────────────────────────────

def extract_dw(elem, flow_name, counter):
    results = []

    def _scan(node, location_hint=''):
        for child in node:
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
                            'has_custom_functions': has_custom_dw_functions(expr),
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
                            'has_custom_functions': has_custom_dw_functions(expr),
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
                            'has_custom_functions': has_custom_dw_functions(expr),
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
                            'has_custom_functions': has_custom_dw_functions(expr),
                            'raw_expression': expr,
                        })

            _scan(child, location_hint)

    _scan(elem)
    return results


# ── Error handler extractor ───────────────────────────────────────────────────

def extract_error_handlers(elem, flow_name, is_global=False):
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
                'flow':            flow_name,
                'mule_type':       err_type,
                'strategy':        strategy,
                'exception_class': info[0],
                'http_status':     info[1],
                'error_code':      info[2],
                'is_global':       is_global,
            })
    return handlers


# ── Flow-ref tracker (P0) ─────────────────────────────────────────────────────

def extract_flow_refs(elem, known_flow_names):
    """
    Return (refs_list, warnings_list).
    refs_list: names of flows referenced via flow-ref.
    warnings_list: refs that are not in known_flow_names (cross-file or missing).
    """
    refs, warnings = [], []
    for fr in elem.iter(tag('mule', 'flow-ref')):
        ref_name = fr.get('name', '')
        if ref_name:
            refs.append(ref_name)
            if ref_name not in known_flow_names:
                warnings.append(f"flow-ref '{ref_name}' not found in this file — likely in imported XML or sub-flow")
    return refs, warnings


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_mule_xml(xml_path):
    tree = ET.parse(xml_path)
    root = tree.getroot()

    flows           = []
    all_dw          = []
    all_errors      = []
    tables          = {}
    jms_destinations = []
    constructs      = set()
    dw_counter      = [0]
    has_batch       = False
    has_scatter_gather = False
    unknown_connectors = []

    http_client_configs, http_listener_configs = extract_http_configs(root)

    imported_files = []
    for imp in root.findall('{http://www.mulesoft.org/schema/mule/core}import'):
        imported_files.append(imp.get('file', ''))
    for imp in root.iter():
        if imp.tag.endswith('}import') and 'file' in imp.attrib:
            f = imp.get('file', '')
            if f and f not in imported_files:
                imported_files.append(f)

    has_apikit = root.find(f'.//{tag("apikit","router")}') is not None
    if has_apikit:
        constructs.add('apikit:router')

    apikit_base_path = '/'
    raml_ref = None
    for ak_config in root.iter(tag('apikit', 'config')):
        apikit_base_path = ak_config.get('api', '/').split('/')[-1]
        raml_ref = ak_config.get('api', None)

    all_flow_names = set()
    for fe in root.findall(tag('mule', 'flow')):
        all_flow_names.add(fe.get('name', ''))
    for fe in root.findall(tag('mule', 'sub-flow')):
        all_flow_names.add(fe.get('name', ''))

    flow_graph_refs = {}
    flow_graph_warnings = []

    def process_flow_elem(flow_elem, is_sub_flow=False):
        nonlocal has_batch, has_scatter_gather

        flow_name = flow_elem.get('name', '')
        trigger = None

        if not is_sub_flow:
            listener = flow_elem.find(tag('http', 'listener'))
            if listener is not None:
                constructs.add('http:listener')
                path_attr   = listener.get('path', '/')
                method_attr = listener.get('method', 'GET').upper()
                config_ref  = listener.get('config-ref', '')
                trigger = {
                    'type':       'http:listener',
                    'method':     method_attr,
                    'path':       path_attr,
                    'config_ref': config_ref,
                }

            if trigger is None:
                method, path = parse_flow_trigger_from_name(flow_name)
                if method:
                    constructs.add('http:listener')
                    trigger = {'type': 'http:listener', 'method': method, 'path': path, 'config_ref': ''}

            for sched in flow_elem.iter(tag('scheduler', 'scheduler')):
                constructs.add('scheduler')
                if trigger is None:
                    trigger = {'type': 'scheduler', 'expression': sched.get('doc:name', 'scheduler')}

            batch_job = flow_elem.find(f'.//{tag("batch","job")}')
            if batch_job is not None:
                has_batch = True
                constructs.add('batch:job')
                if trigger is None:
                    trigger = {'type': 'batch:job', 'name': batch_job.get('jobName', flow_name)}

        if flow_elem.find(f'.//{tag("mule","scatter-gather")}') is not None:
            has_scatter_gather = True
            constructs.add('scatter-gather')

        for db_op in ('select', 'insert', 'update', 'delete', 'bulk-insert', 'bulk-update', 'stored-procedure'):
            for db_elem in flow_elem.iter(tag('db', db_op)):
                constructs.add(f'db:{db_op.split("-")[0]}')
                sql_elem = db_elem.find(tag('db', 'sql'))
                sql = get_text(sql_elem)
                if sql:
                    t_list, cols = extract_sql_meta(sql)
                    for t in t_list:
                        if t not in tables or (cols and not tables[t]):
                            tables[t] = cols

        for jms_pub in flow_elem.iter(tag('jms', 'publish')):
            constructs.add('jms:publish')
            dest = jms_pub.get('destination', 'unknown')
            if dest not in jms_destinations:
                jms_destinations.append(dest)
        for jms_consume in flow_elem.iter(tag('jms', 'consume')):
            constructs.add('jms:consume')
        for jms_listener in flow_elem.iter(tag('jms', 'listener')):
            constructs.add('jms:listener')
            dest = jms_listener.get('destination', 'unknown')
            if dest not in jms_destinations:
                jms_destinations.append(dest)

        for req in flow_elem.iter(tag('http', 'request')):
            constructs.add('http:request')

        if flow_elem.find(f'.//{tag("vm","publish")}') is not None or \
           flow_elem.find(f'.//{tag("vm","consume")}') is not None:
            constructs.add('vm:queue')

        unknown_connectors.extend(detect_unknown_connectors(flow_elem, flow_name))

        refs, warns = extract_flow_refs(flow_elem, all_flow_names)
        if refs:
            flow_graph_refs[flow_name] = refs
        flow_graph_warnings.extend(warns)

        dw_exprs = extract_dw(flow_elem, flow_name, dw_counter)
        all_dw.extend(dw_exprs)

        errors = extract_error_handlers(flow_elem, flow_name, is_global=False)
        all_errors.extend(errors)

        if trigger or is_sub_flow:
            flows.append({
                'name':       flow_name,
                'trigger':    trigger,
                'is_sub_flow': is_sub_flow,
                'processors': list(constructs),
                'refs':       refs,
            })

    for flow_elem in root.findall(tag('mule', 'flow')):
        process_flow_elem(flow_elem, is_sub_flow=False)

    for flow_elem in root.findall(tag('mule', 'sub-flow')):
        process_flow_elem(flow_elem, is_sub_flow=True)

    for geh in root.findall(tag('mule', 'error-handler')):
        errors = extract_error_handlers(geh, 'global', is_global=True)
        all_errors.extend(errors)

    entities = []
    for tbl, cols in tables.items():
        tbl_clean = tbl.lower()
        singular = tbl_clean.rstrip('s') if tbl_clean.endswith('s') and len(tbl_clean) > 3 else tbl_clean
        entity_name = kebab_to_pascal(singular)
        fields = [
            {
                'column':   col,
                'field':    snake_to_camel(col),
                'java_type': infer_java_type(col),
                'pk':       col == 'id',
                'generated': col == 'id',
                'nullable': col not in ('id',),
            }
            for col in cols
        ]
        entities.append({'name': entity_name, 'table': tbl, 'fields': fields})

    score  = len([f for f in flows if not f.get('is_sub_flow')])
    score += 5 if has_batch else 0
    score += 3 if has_scatter_gather else 0
    score += len(set(c.split(':')[0] for c in constructs if ':' in c))
    score += 2 * sum(1 for dw in all_dw if dw['classification'] in ('structural', 'aggregation'))
    score += len(unknown_connectors) * 2
    tier = 'T1' if score <= 6 else ('T2' if score <= 14 else 'T3')

    llm_needed  = [dw for dw in all_dw if dw['send_to_llm']]
    field_maps  = [dw for dw in llm_needed if dw['classification'] == 'field-mapping']
    structural  = [dw for dw in llm_needed if dw['classification'] in ('structural', 'aggregation')]
    llm_calls   = max(1, -(-len(field_maps) // 3)) + len(structural) + len(set(
        c['namespace'] for c in unknown_connectors
    ))

    return {
        'app': '',
        'complexity_tier': tier,
        'constructs': sorted(constructs),
        'flows': flows,
        'entities': entities,
        'connectors': {
            'db':            {'present': bool(tables),            'tables': list(tables.keys())},
            'jms':           {'present': bool(jms_destinations),  'destinations': jms_destinations},
            'http_client':   {'present': 'http:request' in constructs},
            'batch':         {'present': has_batch},
            'scatter_gather':{'present': has_scatter_gather},
            'vm':            {'present': 'vm:queue' in constructs},
            'scheduler':     {'present': 'scheduler' in constructs},
        },
        'http_client_configs':  http_client_configs,
        'http_listener_configs': http_listener_configs,
        'dataweave': all_dw,
        'error_handlers': all_errors,
        'has_apikit': has_apikit,
        'raml_ref': raml_ref,
        'apikit_base_path': apikit_base_path,
        'unknown_connectors': unknown_connectors,
        'flow_graph': {
            'refs':     flow_graph_refs,
            'warnings': flow_graph_warnings,
        },
        'imported_files': imported_files,
        'llm_calls_estimate': llm_calls,
    }


def main():
    parser = argparse.ArgumentParser(description='Parse Mule 4 XML → ir.json')
    parser.add_argument('--app',          required=True)
    parser.add_argument('--project-root', default='.')
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    mule_dir     = project_root / 'mule-apps' / args.app / 'src' / 'main' / 'mule'
    out_dir      = project_root / 'output' / args.app
    out_dir.mkdir(parents=True, exist_ok=True)

    xml_files = list(mule_dir.glob('*.xml'))
    if not xml_files:
        print(f'ERROR: No XML files found in {mule_dir}', file=sys.stderr)
        sys.exit(1)

    merged = {
        'app': args.app,
        'constructs': set(),
        'flows': [], 'entities': [], 'dataweave': [],
        'error_handlers': [], 'unknown_connectors': [],
        'http_client_configs': {}, 'http_listener_configs': {},
        'imported_files': [], 'raml_ref': None,
        'flow_graph': {'refs': {}, 'warnings': []},
        'connectors': {
            'db':            {'present': False, 'tables': []},
            'jms':           {'present': False, 'destinations': []},
            'http_client':   {'present': False},
            'batch':         {'present': False},
            'scatter_gather':{'present': False},
            'vm':            {'present': False},
            'scheduler':     {'present': False},
        },
        'has_apikit': False,
    }

    for xml_file in sorted(xml_files):
        data = parse_mule_xml(xml_file)
        merged['constructs'].update(data['constructs'])
        merged['flows'].extend(data['flows'])
        merged['entities'].extend(data['entities'])
        merged['dataweave'].extend(data['dataweave'])
        merged['error_handlers'].extend(data['error_handlers'])
        merged['unknown_connectors'].extend(data['unknown_connectors'])
        merged['http_client_configs'].update(data['http_client_configs'])
        merged['http_listener_configs'].update(data['http_listener_configs'])
        merged['imported_files'].extend(data['imported_files'])
        merged['flow_graph']['refs'].update(data['flow_graph']['refs'])
        merged['flow_graph']['warnings'].extend(data['flow_graph']['warnings'])
        merged['has_apikit'] = merged['has_apikit'] or data['has_apikit']
        if data.get('raml_ref'):
            merged['raml_ref'] = data['raml_ref']

        for k in ('db', 'jms', 'http_client', 'batch', 'scatter_gather', 'vm', 'scheduler'):
            if data['connectors'][k]['present']:
                merged['connectors'][k]['present'] = True
            if k in ('db', 'jms'):
                key = 'tables' if k == 'db' else 'destinations'
                existing = merged['connectors'][k].get(key, [])
                new_items = data['connectors'][k].get(key, [])
                merged['connectors'][k][key] = list(dict.fromkeys(existing + new_items))

    merged['constructs'] = sorted(merged['constructs'])

    score  = len([f for f in merged['flows'] if not f.get('is_sub_flow')])
    score += 5 if merged['connectors']['batch']['present'] else 0
    score += 3 if merged['connectors']['scatter_gather']['present'] else 0
    score += len(set(c.split(':')[0] for c in merged['constructs'] if ':' in c))
    score += 2 * sum(1 for dw in merged['dataweave'] if dw['classification'] in ('structural', 'aggregation'))
    score += len(set(c['namespace'] for c in merged['unknown_connectors'])) * 2
    merged['complexity_tier'] = 'T1' if score <= 6 else ('T2' if score <= 14 else 'T3')

    llm_dw     = [dw for dw in merged['dataweave'] if dw['send_to_llm']]
    field_maps = [dw for dw in llm_dw if dw['classification'] == 'field-mapping']
    structural = [dw for dw in llm_dw if dw['classification'] in ('structural', 'aggregation')]
    merged['llm_calls_estimate'] = (
        max(1, -(-len(field_maps) // 3)) + len(structural) +
        len(set(c['namespace'] for c in merged['unknown_connectors']))
    )

    out_path = out_dir / 'ir.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(merged, f, indent=2)

    format_only  = sum(1 for dw in merged['dataweave'] if not dw['send_to_llm'])
    llm_needed   = sum(1 for dw in merged['dataweave'] if dw['send_to_llm'])
    custom_dw    = sum(1 for dw in merged['dataweave'] if dw.get('has_custom_functions'))
    unknown_ns   = len(set(c['namespace'] for c in merged['unknown_connectors']))

    print(f'\n── Analysis: {args.app} {"─" * (40 - len(args.app))}')
    print(f'  Flows:          {len([f for f in merged["flows"] if not f.get("is_sub_flow")])}  '
          f'(+{len([f for f in merged["flows"] if f.get("is_sub_flow")])} sub-flows)')
    print(f'  Constructs:     {", ".join(merged["constructs"])}')
    print(f'  DataWeave:      {len(merged["dataweave"])} expressions '
          f'({format_only} format-only → skip LLM, {llm_needed} need translation)')
    if custom_dw:
        print(f'  Custom DW fns:  {custom_dw} (flagged for enrich.py AST analysis)')
    if merged['unknown_connectors']:
        print(f'  Unknown conn:   {unknown_ns} namespace(s) — {len(merged["unknown_connectors"])} usages '
              f'→ LLM stub generation')
    if merged['flow_graph']['warnings']:
        for w in merged['flow_graph']['warnings'][:3]:
            print(f'  ⚠ {w}')
    if merged['raml_ref']:
        print(f'  RAML ref:       {merged["raml_ref"]} → run raml_reader.py')
    print(f'  LLM calls est:  ~{merged["llm_calls_estimate"]} (after batching)')
    print(f'  Tier:           {merged["complexity_tier"]}')
    print(f'  IR saved:       {out_path}')
    print('─' * 50)


if __name__ == '__main__':
    main()
