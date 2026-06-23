#!/usr/bin/env python3
"""
Stage 2 — Generate Spring Boot scaffold from ir.json + migration-strategy.yaml
No LLM. Pure Jinja2 template rendering.

Usage:
    python tool/generate.py --app order-api [--project-root .] [--toolkit-root ./mule-migration-toolkit]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader


# ── Name helpers ──────────────────────────────────────────────────────────────

def snake_to_pascal(s):
    return ''.join(p.title() for p in re.split(r'[-_]', s))

def snake_to_camel(s):
    parts = re.split(r'[-_]', s)
    return parts[0].lower() + ''.join(p.title() for p in parts[1:])

def app_to_class_prefix(app_name):
    """order-api → Order, invoice-batch → Invoice, notification-api → Notification"""
    return snake_to_pascal(app_name.split('-')[0])

def app_to_package_suffix(app_name):
    """order-api → orderapi"""
    return re.sub(r'[^a-z0-9]', '', app_name.lower())

def http_annotation(method):
    return {
        'GET': 'GetMapping', 'POST': 'PostMapping',
        'PUT': 'PutMapping', 'DELETE': 'DeleteMapping', 'PATCH': 'PatchMapping',
    }.get(method.upper(), 'RequestMapping')

def path_to_sub_path(full_path, base_path):
    """'/orders/{id}' with base '/orders' → '/{id}'"""
    sub = full_path[len(base_path):]
    return sub if sub else ''

def extract_path_params(path):
    return re.findall(r'\{(\w+)\}', path)

def derive_base_path(flows):
    """Find the common path prefix across all HTTP flows."""
    http_paths = [f['trigger']['path'] for f in flows if f['trigger']['type'] == 'http:listener']
    if not http_paths:
        return '/'
    parts = [p.split('/') for p in http_paths]
    common = []
    for segs in zip(*parts):
        if len(set(segs)) == 1 and '{' not in segs[0]:
            common.append(segs[0])
        else:
            break
    return '/' + '/'.join(filter(None, common))

def infer_request_type(flow, class_prefix):
    if flow['trigger'].get('method') == 'POST':
        return f'Create{class_prefix}Request'
    return None

def infer_response_type(flow, class_prefix):
    method = flow['trigger'].get('method', 'GET')
    path   = flow['trigger'].get('path', '')
    if method == 'POST':
        return f'Create{class_prefix}Response'
    if '{' in path:
        return class_prefix + 'Response'
    return f'List<{class_prefix}Response>'

def infer_operation_id(flow):
    method = flow['trigger'].get('method', 'GET').lower()
    path   = flow['trigger'].get('path', '/')
    has_id = '{' in path
    base   = path.strip('/').split('/')[0]
    base_pascal = snake_to_pascal(base)
    if method == 'get' and has_id:    return f'get{base_pascal}'
    if method == 'get':               return f'list{base_pascal}s'
    if method == 'post':              return f'create{base_pascal}'
    if method == 'put':               return f'update{base_pascal}'
    if method == 'delete':            return f'delete{base_pascal}'
    return f'{method}{base_pascal}'

def unique_error_handlers(error_handlers):
    """Deduplicate error handlers by exception_class, global last."""
    seen = {}
    for eh in error_handlers:
        cls = eh['exception_class']
        if cls == 'Exception':
            seen['__any__'] = eh
        elif cls not in seen:
            seen[cls] = eh
    result = [v for k, v in seen.items() if k != '__any__']
    if '__any__' in seen:
        result.append(seen['__any__'])
    return result

def mule_error_to_spring_exception(mule_type):
    from parse import ERROR_MAP
    return ERROR_MAP.get(mule_type, ('ApiException', 500, 'API_ERROR'))


# ── Template context builder ──────────────────────────────────────────────────

def build_context(ir, strategy):
    app_name     = ir['app']
    pkg_root     = strategy['architecture']['package_root']
    pkg_suffix   = app_to_package_suffix(app_name)
    package      = f'{pkg_root}.{pkg_suffix}'
    class_prefix = app_to_class_prefix(app_name)

    http_flows = [f for f in ir['flows'] if f['trigger']['type'] == 'http:listener']
    base_path  = derive_base_path(http_flows)

    endpoints = []
    for flow in http_flows:
        method      = flow['trigger'].get('method', 'GET')
        full_path   = flow['trigger'].get('path', '/')
        sub_path    = path_to_sub_path(full_path, base_path)
        path_params = extract_path_params(full_path)
        op_id       = infer_operation_id(flow)
        req_type    = infer_request_type(flow, class_prefix)
        resp_type   = infer_response_type(flow, class_prefix)
        # DataWeave IDs for this flow (for TODO comments)
        dw_ids = [dw['id'] for dw in ir['dataweave'] if dw['flow'] == flow['name'] and dw['send_to_llm']]

        endpoints.append({
            'method':        method,
            'annotation':    http_annotation(method),
            'full_path':     full_path,
            'sub_path':      sub_path or None,
            'path_params':   path_params,
            'has_body':      method in ('POST', 'PUT', 'PATCH'),
            'operation_id':  op_id,
            'request_type':  req_type,
            'response_type': resp_type,
            'dw_ids':        dw_ids,
        })

    sub_exceptions = unique_error_handlers([
        eh for eh in ir['error_handlers']
        if eh['exception_class'] not in ('Exception', 'ApiException')
    ])

    return {
        'ir':             ir,
        'strategy':       strategy,
        'app_name':       app_name,
        'package':        package,
        'pkg_root':       pkg_root,
        'pkg_suffix':     pkg_suffix,
        'class_prefix':   class_prefix,
        'base_path':      base_path,
        'endpoints':      endpoints,
        'http_flows':     http_flows,
        'entities':       ir.get('entities', []),
        'error_handlers': unique_error_handlers(ir.get('error_handlers', [])),
        'sub_exceptions': sub_exceptions,
        'has_db':         ir['connectors']['db']['present'],
        'has_jms':        ir['connectors']['jms']['present'],
        'has_batch':      ir['connectors']['batch']['present'],
        'has_scatter':    ir['connectors']['scatter_gather']['present'],
        'has_http_client':ir['connectors']['http_client']['present'],
        'jms_destinations': ir['connectors']['jms'].get('destinations', []),
        'db_tables':      ir['connectors']['db'].get('tables', []),
        'java_version':   strategy['project']['java_version'],
        'sb_version':     strategy['project']['spring_boot_version'],
        'jms_client':     strategy['messaging']['jms_client'],
        'jms_broker':     strategy['messaging']['jms_broker'],
        'db_access':      strategy['data']['db_access'],
        'error_strategy': strategy['error_handling']['strategy'],
        'exc_base':       strategy['error_handling']['exception_base_class'],
        'resilience':     strategy['resilience']['retry'],
        'test_style':     strategy['testing']['integration'],
    }


# ── File plan ─────────────────────────────────────────────────────────────────

def file_plan(ctx, template_dir, out_dir):
    """Return list of (template_name, output_path, extra_ctx)."""
    pkg_path = ctx['package'].replace('.', os.sep)
    java_main = out_dir / 'src' / 'main' / 'java' / pkg_path
    java_test = out_dir / 'src' / 'test' / 'java' / pkg_path
    resources  = out_dir / 'src' / 'main' / 'resources'
    plan = []

    plan.append(('pom.xml.j2',           out_dir / 'pom.xml',          {}))
    plan.append(('application.yml.j2',   resources / 'application.yml', {}))
    plan.append(('Application.java.j2',  java_main / f'{snake_to_pascal(ctx["app_name"])}Application.java', {}))

    if ctx['http_flows']:
        plan.append(('Controller.java.j2', java_main / 'controller' / f'{ctx["class_prefix"]}Controller.java', {}))
        plan.append(('Service.java.j2',    java_main / 'service'    / f'{ctx["class_prefix"]}Service.java',    {}))
        plan.append(('Mapper.java.j2',     java_main / 'mapper'     / f'{ctx["class_prefix"]}Mapper.java',     {}))

    for entity in ctx['entities']:
        plan.append(('Entity.java.j2',     java_main / 'domain'     / f'{entity["name"]}.java',           {'entity': entity}))
        plan.append(('Repository.java.j2', java_main / 'repository' / f'{entity["name"]}Repository.java', {'entity': entity}))

    if ctx['has_jms']:
        plan.append(('JmsConfig.java.j2',  java_main / 'messaging' / 'JmsConfig.java', {}))

    plan.append(('ApiException.java.j2', java_main / 'exception' / f'{ctx["exc_base"]}.java', {}))
    for sub_ex in ctx['sub_exceptions']:
        if sub_ex['exception_class'] not in ('Exception', ctx['exc_base']):
            plan.append(('SubException.java.j2', java_main / 'exception' / f'{sub_ex["exception_class"]}.java',
                         {'sub_ex': sub_ex}))
    plan.append(('GlobalExceptionHandler.java.j2', java_main / 'exception' / 'GlobalExceptionHandler.java', {}))

    return plan


# ── Renderer ──────────────────────────────────────────────────────────────────

def render_all(ctx, template_dir, out_dir):
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters['pascal'] = snake_to_pascal
    env.filters['camel']  = snake_to_camel

    plan = file_plan(ctx, template_dir, out_dir)
    generated = []

    for tmpl_name, out_path, extra in plan:
        try:
            tmpl = env.get_template(tmpl_name)
        except Exception as e:
            print(f'  SKIP {tmpl_name}: {e}')
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        merged_ctx = {**ctx, **extra}
        content = tmpl.render(**merged_ctx)
        out_path.write_text(content, encoding='utf-8')
        generated.append(str(out_path.relative_to(out_dir)))

    return generated


def main():
    parser = argparse.ArgumentParser(description='Generate Spring Boot scaffold from ir.json')
    parser.add_argument('--app',           required=True)
    parser.add_argument('--project-root',  default='.')
    parser.add_argument('--toolkit-root',  default='./mule-migration-toolkit')
    args = parser.parse_args()

    project_root  = Path(args.project_root).resolve()
    toolkit_root  = Path(args.toolkit_root).resolve()
    template_dir  = toolkit_root / 'tool' / 'templates'
    strategy_file = toolkit_root / 'migration-strategy.yaml'
    ir_path       = project_root / 'output' / args.app / 'ir.json'
    out_dir       = project_root / 'output' / args.app

    for p, label in [(ir_path, 'ir.json'), (strategy_file, 'migration-strategy.yaml'), (template_dir, 'templates/')]:
        if not p.exists():
            print(f'ERROR: {label} not found at {p}', file=sys.stderr)
            sys.exit(1)

    ir       = json.loads(ir_path.read_text(encoding='utf-8'))
    strategy = yaml.safe_load(strategy_file.read_text(encoding='utf-8'))
    ctx      = build_context(ir, strategy)

    print(f'\n── Generating: {args.app} {"─" * (38 - len(args.app))}')
    generated = render_all(ctx, template_dir, out_dir)
    for f in generated:
        print(f'  ✓ {f}')
    print(f'── {len(generated)} files generated (0 LLM tokens) ──')
    print()


if __name__ == '__main__':
    main()
