#!/usr/bin/env python3
"""
Stage 0 — Parse RAML 1.0 or OAS 3.0 API spec → raml_ir.json

Produces authoritative:
  - Type definitions → Java records/enums (POJOs)
  - Endpoint contract → overrides flow-name inference in generate.py

Usage:
    python raml_reader.py --app order-api [--project-root .]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


# ── Java type mapping ───────────────────────────────────────────────────────

def raml_scalar_to_java(raml_type, field_name='', format_hint=''):
    """Map a RAML scalar type string to a Java type string."""
    t = (raml_type or 'string').lower().strip()
    f = (format_hint or '').lower()
    n = (field_name or '').lower()

    if t.endswith('[]'):
        inner = raml_scalar_to_java(t[:-2], field_name)
        return f'List<{inner}>'

    if t in ('integer', 'int'):
        if n.endswith('id') or n == 'id':
            return 'Long'
        if f == 'int64':
            return 'Long'
        return 'Integer'
    if t in ('long',):
        return 'Long'
    if t in ('number', 'float', 'double'):
        return 'BigDecimal'
    if t in ('boolean', 'bool'):
        return 'Boolean'
    if t in ('datetime', 'datetime-only'):
        return 'LocalDateTime'
    if t in ('date', 'date-only'):
        return 'LocalDate'
    if t in ('time', 'time-only'):
        return 'LocalTime'
    if t in ('string', 'str', 'nil', 'null', 'any'):
        return 'String'
    if t == 'file':
        return 'MultipartFile'
    return pascal_case(t)


def oas_schema_to_java(schema, field_name='', all_schemas=None):
    """Recursively map an OAS schema object to a Java type string."""
    if schema is None:
        return 'Object'
    all_schemas = all_schemas or {}

    if '$ref' in schema:
        ref = schema['$ref']
        class_name = ref.split('/')[-1]
        return pascal_case(class_name)

    t    = schema.get('type', 'object')
    fmt  = schema.get('format', '')
    n    = field_name.lower()
    enum = schema.get('enum')

    if enum:
        return pascal_case(field_name) + 'Enum'

    if t == 'array':
        items = schema.get('items', {})
        inner = oas_schema_to_java(items, field_name, all_schemas)
        return f'List<{inner}>'
    if t == 'integer':
        if n.endswith('id') or n == 'id' or fmt == 'int64':
            return 'Long'
        return 'Integer'
    if t == 'number':
        return 'BigDecimal'
    if t == 'boolean':
        return 'Boolean'
    if t == 'string':
        if fmt in ('date-time', 'datetime'):
            return 'LocalDateTime'
        if fmt == 'date':
            return 'LocalDate'
        if fmt == 'time':
            return 'LocalTime'
        if fmt in ('binary', 'byte'):
            return 'byte[]'
        return 'String'
    if t == 'object':
        if field_name:
            return pascal_case(field_name)
        return 'Map<String, Object>'

    return 'Object'


# ── Name helpers ────────────────────────────────────────────────────────────

def pascal_case(s):
    if not s:
        return 'Unknown'
    return ''.join(p.title() for p in re.split(r'[-_\s]', s))

def camel_case(s):
    if not s:
        return 'unknown'
    parts = re.split(r'[-_\s]', s)
    return parts[0][0].lower() + parts[0][1:] + ''.join(p.title() for p in parts[1:])

def operation_id_from_method_path(method, path):
    segments = [s for s in path.strip('/').split('/') if s and not s.startswith('{')]
    base = pascal_case(segments[0]) if segments else 'Resource'
    has_id = '{' in path
    m = method.lower()
    if m == 'get' and has_id:   return f'get{base}'
    if m == 'get':              return f'list{base}s'
    if m == 'post':             return f'create{base}'
    if m == 'put':              return f'update{base}'
    if m == 'delete':           return f'delete{base}'
    if m == 'patch':            return f'patch{base}'
    return f'{m}{base}'


# ── RAML 1.0 parser ────────────────────────────────────────────────────────────

def parse_raml_property(name, prop_def, required_list):
    """Parse a single RAML type property into a field descriptor."""
    if prop_def is None:
        prop_def = {}

    required = name in (required_list or [])
    actual_name = name.rstrip('?')
    if name.endswith('?'):
        required = False
    else:
        required = actual_name in (required_list or []) or required

    if isinstance(prop_def, str):
        java_type = raml_scalar_to_java(prop_def, actual_name)
        return {
            'name':          actual_name,
            'java_name':     camel_case(actual_name),
            'json_property': actual_name,
            'java_type':     java_type,
            'required':      required,
            'nullable':      not required,
            'description':   '',
        }

    prop_type    = prop_def.get('type', 'string')
    items_type   = prop_def.get('items', None)
    enum_values  = prop_def.get('enum', None)
    description  = prop_def.get('description', '')
    format_hint  = prop_def.get('format', '')

    if prop_type == 'array' and items_type:
        inner = raml_scalar_to_java(items_type if isinstance(items_type, str)
                                    else items_type.get('type', 'string'), actual_name)
        java_type = f'List<{inner}>'
    elif enum_values:
        java_type = pascal_case(actual_name) + 'Type'
    elif prop_type == 'object':
        java_type = pascal_case(actual_name)
    else:
        java_type = raml_scalar_to_java(prop_type, actual_name, format_hint)

    return {
        'name':          actual_name,
        'java_name':     camel_case(actual_name),
        'json_property': actual_name,
        'java_type':     java_type,
        'required':      required,
        'nullable':      not required,
        'enum_values':   enum_values,
        'description':   description,
    }


def parse_raml_type(type_name, type_def):
    """Parse a RAML type definition into a Java class descriptor."""
    if isinstance(type_def, str):
        return {
            'name':       pascal_case(type_name),
            'raml_name':  type_name,
            'kind':       'alias',
            'alias_of':   raml_scalar_to_java(type_def, type_name),
            'fields':     [],
            'generate':   'skip',
        }

    if not isinstance(type_def, dict):
        return None

    parent_type  = type_def.get('type', 'object')
    properties   = type_def.get('properties', {})
    required     = type_def.get('required', [])
    enum_values  = type_def.get('enum', None)
    description  = type_def.get('description', '')

    if enum_values:
        return {
            'name':        pascal_case(type_name),
            'raml_name':   type_name,
            'kind':        'enum',
            'enum_values': enum_values,
            'description': description,
            'fields':      [],
            'generate':    'enum',
        }

    if parent_type == 'array':
        items = type_def.get('items', 'string')
        return {
            'name':      pascal_case(type_name),
            'raml_name': type_name,
            'kind':      'array',
            'item_type': raml_scalar_to_java(items if isinstance(items, str) else items.get('type', 'string'), type_name),
            'fields':    [],
            'generate':  'skip',
        }

    if parent_type not in ('object', None, '') and not properties:
        return {
            'name':      pascal_case(type_name),
            'raml_name': type_name,
            'kind':      'alias',
            'alias_of':  raml_scalar_to_java(parent_type, type_name),
            'fields':    [],
            'generate':  'skip',
        }

    fields = []
    for prop_name, prop_def in (properties or {}).items():
        field = parse_raml_property(prop_name, prop_def, required)
        if field:
            fields.append(field)

    extends_class = None
    if parent_type and parent_type != 'object':
        extends_class = pascal_case(parent_type)

    return {
        'name':          pascal_case(type_name),
        'raml_name':     type_name,
        'kind':          'object',
        'fields':        fields,
        'extends':       extends_class,
        'description':   description,
        'generate':      'record',
    }


def parse_raml_body(body_def):
    if not body_def:
        return None
    content = body_def.get('application/json', body_def.get('application/*', None))
    if content is None:
        return None
    schema_ref = content.get('type', content.get('schema', None))
    if not schema_ref:
        return None
    if isinstance(schema_ref, str):
        if schema_ref.endswith('[]'):
            return f'List<{pascal_case(schema_ref[:-2])}>'
        return pascal_case(schema_ref)
    return None


def parse_raml_responses(responses_def):
    if not responses_def:
        return 200, None
    for code in (200, 201, 202, 204):
        resp = responses_def.get(code)
        if resp:
            body = resp.get('body', {})
            java_type = parse_raml_body(body)
            return code, java_type
    for code, resp in responses_def.items():
        if isinstance(code, int) and 200 <= code < 300:
            body = resp.get('body', {}) if resp else {}
            java_type = parse_raml_body(body)
            return code, java_type
    return 200, None


def parse_raml_resources(resources, base_path='', path_params_inherited=None):
    """Recursively parse RAML resources → endpoint list."""
    endpoints = []
    path_params_inherited = path_params_inherited or []

    for path_segment, resource_def in (resources or {}).items():
        if not path_segment.startswith('/'):
            continue
        full_path = base_path + path_segment

        uri_params = list(path_params_inherited)
        for param_name, param_def in resource_def.get('uriParameters', {}).items():
            uri_params.append({
                'name':      param_name,
                'java_name': camel_case(param_name),
                'java_type': raml_scalar_to_java(
                    param_def.get('type', 'string') if isinstance(param_def, dict) else 'string',
                    param_name
                ),
                'required': True,
            })

        for method in ('get', 'post', 'put', 'delete', 'patch', 'head', 'options'):
            method_def = resource_def.get(method)
            if not method_def:
                continue

            query_params = []
            for qp_name, qp_def in (method_def.get('queryParameters') or {}).items():
                qp_def = qp_def or {}
                if isinstance(qp_def, str):
                    qp_def = {'type': qp_def}
                query_params.append({
                    'name':      qp_name,
                    'java_name': camel_case(qp_name),
                    'java_type': raml_scalar_to_java(qp_def.get('type', 'string'), qp_name),
                    'required':  qp_def.get('required', False),
                })

            request_type = parse_raml_body(method_def.get('body'))
            status_code, response_type = parse_raml_responses(method_def.get('responses'))

            op_id = (method_def.get('operationId') or
                     method_def.get('displayName') or
                     operation_id_from_method_path(method, full_path))

            endpoints.append({
                'method':        method.upper(),
                'path':          full_path,
                'operation_id':  camel_case(op_id),
                'path_params':   [p for p in uri_params],
                'query_params':  query_params,
                'has_body':      method in ('post', 'put', 'patch'),
                'request_type':  request_type,
                'response_type': response_type,
                'response_status': status_code,
                'description':   method_def.get('description', ''),
            })

        nested = {k: v for k, v in resource_def.items()
                  if k.startswith('/') and isinstance(v, dict)}
        if nested:
            endpoints.extend(parse_raml_resources(nested, full_path, uri_params))

    return endpoints


def read_raml(spec_path):
    content = spec_path.read_text(encoding='utf-8')
    if not content.lstrip().startswith('#%RAML'):
        return None

    lines = content.splitlines()
    yaml_lines = [l for l in lines if not l.strip().startswith('#%RAML')]
    data = yaml.safe_load('\n'.join(yaml_lines)) or {}

    base_uri  = data.get('baseUri', '/api')
    version   = data.get('version', 'v1')
    base_path = re.sub(r'\{version\}', version, base_uri)
    base_path = re.sub(r'^https?://[^/]+', '', base_path)

    types_raw = data.get('types', data.get('schemas', {}))
    types = []
    for type_name, type_def in (types_raw or {}).items():
        t = parse_raml_type(type_name, type_def)
        if t and t['generate'] != 'skip':
            types.append(t)

    resources = {k: v for k, v in data.items()
                 if k.startswith('/') and isinstance(v, dict)}
    endpoints = parse_raml_resources(resources)

    return {
        'format':    'raml10',
        'title':     data.get('title', ''),
        'version':   version,
        'base_path': base_path,
        'types':     types,
        'endpoints': endpoints,
    }


# ── OAS 3.0 parser ────────────────────────────────────────────────────────────

def parse_oas_schema(name, schema_def, all_schemas):
    if not schema_def:
        return None

    schema_type = schema_def.get('type', 'object')
    properties  = schema_def.get('properties', {})
    required    = schema_def.get('required', [])
    enum_values = schema_def.get('enum')
    description = schema_def.get('description', '')

    if enum_values:
        return {
            'name':        pascal_case(name),
            'raml_name':   name,
            'kind':        'enum',
            'enum_values': [str(v) for v in enum_values],
            'description': description,
            'fields':      [],
            'generate':    'enum',
        }

    if schema_type == 'array':
        items = schema_def.get('items', {})
        return {
            'name':      pascal_case(name),
            'raml_name': name,
            'kind':      'array',
            'item_type': oas_schema_to_java(items, name, all_schemas),
            'fields':    [],
            'generate':  'skip',
        }

    if schema_type != 'object' and not properties:
        return {
            'name':      pascal_case(name),
            'raml_name': name,
            'kind':      'alias',
            'alias_of':  oas_schema_to_java(schema_def, name, all_schemas),
            'fields':    [],
            'generate':  'skip',
        }

    fields = []
    for prop_name, prop_def in (properties or {}).items():
        java_type = oas_schema_to_java(prop_def, prop_name, all_schemas)
        is_required = prop_name in required
        fields.append({
            'name':          prop_name,
            'java_name':     camel_case(prop_name),
            'json_property': prop_name,
            'java_type':     java_type,
            'required':      is_required,
            'nullable':      not is_required,
            'enum_values':   prop_def.get('enum') if isinstance(prop_def, dict) else None,
            'description':   prop_def.get('description', '') if isinstance(prop_def, dict) else '',
        })

    extends_class = None
    for combinator in ('allOf', 'oneOf', 'anyOf'):
        for entry in schema_def.get(combinator, []):
            if '$ref' in entry:
                extends_class = pascal_case(entry['$ref'].split('/')[-1])
                break
        if extends_class:
            break

    return {
        'name':        pascal_case(name),
        'raml_name':   name,
        'kind':        'object',
        'fields':      fields,
        'extends':     extends_class,
        'description': description,
        'generate':    'record',
    }


def resolve_oas_ref(schema, all_schemas):
    if '$ref' in schema:
        ref_name = schema['$ref'].split('/')[-1]
        return all_schemas.get(ref_name, schema)
    return schema


def parse_oas_operation(method, op_def, path, all_schemas):
    if not op_def:
        return None

    path_params  = []
    query_params = []
    for param in op_def.get('parameters', []):
        param = resolve_oas_ref(param, all_schemas)
        p_in = param.get('in', '')
        p_schema = resolve_oas_ref(param.get('schema', {'type': 'string'}), all_schemas)
        entry = {
            'name':      param.get('name', ''),
            'java_name': camel_case(param.get('name', '')),
            'java_type': oas_schema_to_java(p_schema, param.get('name', ''), all_schemas),
            'required':  param.get('required', p_in == 'path'),
        }
        if p_in == 'path':
            path_params.append(entry)
        elif p_in == 'query':
            query_params.append(entry)

    request_type = None
    req_body = op_def.get('requestBody', {})
    if req_body:
        content = req_body.get('content', {})
        json_content = content.get('application/json', {})
        schema = resolve_oas_ref(json_content.get('schema', {}), all_schemas)
        if '$ref' in json_content.get('schema', {}):
            request_type = pascal_case(json_content['schema']['$ref'].split('/')[-1])
        elif schema.get('type') == 'array':
            inner = oas_schema_to_java(schema.get('items', {}), '', all_schemas)
            request_type = f'List<{inner}>'
        elif schema:
            request_type = oas_schema_to_java(schema, '', all_schemas) or None

    responses = op_def.get('responses', {})
    response_status = 200
    response_type   = None
    for code in (200, 201, 202, 204):
        resp = responses.get(code, responses.get(str(code)))
        if resp:
            content = resp.get('content', {})
            json_content = content.get('application/json', {})
            schema = resolve_oas_ref(json_content.get('schema', {}), all_schemas)
            if '$ref' in json_content.get('schema', {}):
                response_type = pascal_case(json_content['schema']['$ref'].split('/')[-1])
            elif schema.get('type') == 'array':
                inner = oas_schema_to_java(schema.get('items', {}), '', all_schemas)
                response_type = f'List<{inner}>'
            elif schema:
                jt = oas_schema_to_java(schema, '', all_schemas)
                response_type = jt if jt != 'Object' else None
            response_status = code
            break

    op_id = op_def.get('operationId') or operation_id_from_method_path(method, path)

    return {
        'method':          method.upper(),
        'path':            path,
        'operation_id':    camel_case(op_id),
        'path_params':     path_params,
        'query_params':    query_params,
        'has_body':        method.lower() in ('post', 'put', 'patch'),
        'request_type':    request_type,
        'response_type':   response_type,
        'response_status': response_status,
        'description':     op_def.get('summary', op_def.get('description', '')),
    }


def read_oas(spec_path):
    content = spec_path.read_text(encoding='utf-8')
    try:
        data = yaml.safe_load(content)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    if 'openapi' not in data and 'swagger' not in data:
        return None

    all_schemas = (data.get('components', {}) or {}).get('schemas', {})

    types = []
    for schema_name, schema_def in all_schemas.items():
        t = parse_oas_schema(schema_name, schema_def, all_schemas)
        if t and t['generate'] != 'skip':
            types.append(t)

    endpoints = []
    for path, path_def in (data.get('paths', {}) or {}).items():
        if not isinstance(path_def, dict):
            continue
        for method in ('get', 'post', 'put', 'delete', 'patch', 'head', 'options'):
            op_def = path_def.get(method)
            if not op_def:
                continue
            ep = parse_oas_operation(method, op_def, path, all_schemas)
            if ep:
                endpoints.append(ep)

    servers = data.get('servers', [])
    base_path = '/'
    if servers:
        url = servers[0].get('url', '/')
        base_path = re.sub(r'^https?://[^/]+', '', url) or '/'

    info = data.get('info', {})
    return {
        'format':    'oas30',
        'title':     info.get('title', ''),
        'version':   info.get('version', '1.0'),
        'base_path': base_path,
        'types':     types,
        'endpoints': endpoints,
    }


# ── Auto-detect format ──────────────────────────────────────────────────────

def detect_and_read(spec_path):
    content = spec_path.read_text(encoding='utf-8', errors='ignore')
    first_line = content.lstrip().split('\n')[0]

    if first_line.startswith('#%RAML'):
        return read_raml(spec_path)

    try:
        data = yaml.safe_load(content)
        if isinstance(data, dict):
            if 'openapi' in data or 'swagger' in data:
                return read_oas(spec_path)
    except Exception:
        pass

    return None


def find_spec_file(app_dir):
    candidates = [
        app_dir / 'src' / 'main' / 'resources' / 'api' / 'api.raml',
        app_dir / 'src' / 'main' / 'resources' / 'api' / 'api.yaml',
        app_dir / 'src' / 'main' / 'resources' / 'api' / 'api.yml',
        app_dir / 'src' / 'main' / 'resources' / 'api' / 'openapi.yaml',
        app_dir / 'src' / 'main' / 'resources' / 'api' / 'openapi.yml',
    ]
    for ext in ('raml', 'yaml', 'yml'):
        candidates.append(app_dir / f'api.{ext}')
    for p in candidates:
        if p.exists():
            return p
    for p in app_dir.rglob('*.raml'):
        return p
    return None


def main():
    parser = argparse.ArgumentParser(description='Parse RAML/OAS spec → raml_ir.json')
    parser.add_argument('--app',           required=True)
    parser.add_argument('--project-root',  default='.')
    parser.add_argument('--spec-file',     default=None)
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    app_dir      = project_root / 'mule-apps' / args.app
    out_dir      = project_root / 'output' / args.app
    out_dir.mkdir(parents=True, exist_ok=True)

    spec_path = Path(args.spec_file) if args.spec_file else find_spec_file(app_dir)
    if not spec_path or not spec_path.exists():
        print(f'No API spec found for {args.app} — skipping raml_reader (will use flow-name inference)')
        empty = {'format': None, 'title': '', 'version': '', 'base_path': '/', 'types': [], 'endpoints': []}
        (out_dir / 'raml_ir.json').write_text(json.dumps(empty, indent=2), encoding='utf-8')
        return

    result = detect_and_read(spec_path)
    if not result:
        print(f'ERROR: Could not parse {spec_path}', file=sys.stderr)
        sys.exit(1)

    out_path = out_dir / 'raml_ir.json'
    out_path.write_text(json.dumps(result, indent=2), encoding='utf-8')

    print(f'\n── RAML/OAS Reader: {args.app} {"─" * (32 - len(args.app))}')
    print(f'  Format:     {result["format"]}')
    print(f'  Title:      {result["title"]} {result["version"]}')
    print(f'  Base path:  {result["base_path"]}')
    print(f'  Types:      {len(result["types"])} → Java records/enums')
    print(f'  Endpoints:  {len(result["endpoints"])}')
    for ep in result['endpoints']:
        req  = f' ← {ep["request_type"]}' if ep.get('request_type') else ''
        resp = f' → {ep["response_type"]}' if ep.get('response_type') else ''
        print(f'    {ep["method"]:<7} {ep["path"]}{req}{resp}')
    print(f'  Saved:      {out_path}')
    print('─' * 50)


if __name__ == '__main__':
    main()
