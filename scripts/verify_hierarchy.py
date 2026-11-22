import argparse
import asyncio
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--db', default='/tmp/tracekit-hierarchy-verify.db')
args = parser.parse_args()

DB = str(Path(args.db).expanduser().resolve())
os.environ['TRACEKIT_DB_PATH'] = DB
Path(DB).unlink(missing_ok=True)

from langchain_core.language_models.fake import FakeListLLM
from langchain_core.runnables import RunnableSequence
from langchain_core.tools import tool

from tracekit.core.tracer import Tracer
from tracekit.dashboard.server import start_dashboard_server, stop_dashboard_server
from tracekit.integrations.langchain import TracekitCallbackHandler


@tool(description='Simulate a slow tool call')
def slow_operation(value: str) -> str:
    return f'slow:{value}'


@tool(description='Calculate a sum')
def calculate_sum(value: str) -> str:
    return f'sum:{value}'


def llm(response: str):
    return FakeListLLM(responses=[response]).with_config(run_name='GeminiBaseLLM')


nested_chain = RunnableSequence(
    llm('nested result'),
    slow_operation.with_config(run_name='slow_operation'),
).with_config(run_name='nested_chain')

workflow = RunnableSequence(
    llm('calling slow_operation'),
    slow_operation.with_config(run_name='slow_operation'),
    llm('calling calculate_sum'),
    calculate_sum.with_config(run_name='calculate_sum'),
    llm('calling nested_chain'),
    nested_chain,
)

tracer = Tracer(db_path=DB)
handler = TracekitCallbackHandler(tracer=tracer)
with tracer.trace('agent-run') as trace:
    workflow.invoke('sum 2 and 3', config={'callbacks': [handler]})
asyncio.run(tracer.flush())

connection = sqlite3.connect(DB)
connection.row_factory = sqlite3.Row
rows = connection.execute(
    "select span_id, parent_span_id, name, span_type, start_time, end_time "
    "from spans where trace_id = ? order by start_time",
    (trace.trace_id,),
).fetchall()
connection.close()

by_id = {row['span_id']: row for row in rows}
roots = [row for row in rows if row['parent_span_id'] is None]
assert len(roots) == 1, f'expected one root span, got {len(roots)}'
root = roots[0]

for row in rows:
    parent_id = row['parent_span_id']
    if row['span_id'] == root['span_id']:
        continue
    assert parent_id, f'non-root span {row["name"]} has no parent'
    parent = by_id.get(parent_id)
    assert parent is not None, f'parent {parent_id} missing for {row["name"]}'
    assert row['start_time'] >= parent['start_time'], f'{row["name"]} starts before parent'
    assert row['end_time'] <= parent['end_time'], f'{row["name"]} ends after parent'

direct_children = [row for row in rows if row['parent_span_id'] == root['span_id']]
assert sorted(row['name'] for row in direct_children) == [
    'FakeListLLM',
    'FakeListLLM',
    'FakeListLLM',
    'calculate_sum',
    'chain',
    'slow_operation',
]
nested = next(row for row in direct_children if row['span_type'] == 'chain')
nested_children = [row for row in rows if row['parent_span_id'] == nested['span_id']]
assert sorted(row['name'] for row in nested_children) == ['FakeListLLM', 'slow_operation']

server, loop, loop_thread = start_dashboard_server(DB, host='127.0.0.1', port=0, token='test-token')
try:
    url = (
        f'http://127.0.0.1:{server.server_address[1]}/api/traces/{trace.trace_id}/replay'
        '?token=test-token'
    )
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.load(response)
finally:
    stop_dashboard_server(server, loop, loop_thread)

api_by_id = {span['span_id']: span for span in payload['spans']}
assert all('parent_span_id' in span for span in payload['spans'])
assert api_by_id[nested['span_id']]['parent_span_id'] == root['span_id']
assert len(payload['tree']) == 1
assert payload['tree'][0]['span_id'] == root['span_id']
assert {child['span_id'] for child in payload['tree'][0]['children']} == {
    row['span_id'] for row in direct_children
}
nested_api = next(child for child in payload['tree'][0]['children'] if child['span_id'] == nested['span_id'])
assert {child['span_id'] for child in nested_api['children']} == {
    row['span_id'] for row in nested_children
}

print(f'trace_id={trace.trace_id}')
print('span_id           parent_span_id    name')
for row in rows:
    print(f'{row["span_id"]:<17} {str(row["parent_span_id"] or "-"):<17} {row["name"]}')
print(f'api_spans={len(payload["spans"])} api_roots={len(payload["tree"])} nested_children={len(nested_api["children"])}')
print('OK: SDK -> SQLite -> API parent hierarchy verified')
