import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import ts from 'typescript';

const source = await readFile(new URL('../src/utils/spanTree.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2020,
  },
});
const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`;
const {
  MISSING_PARENT_ID,
  buildSpanTree,
  countTreeNodes,
  filterSpanTree,
  flattenVisibleSpans,
  matchingSpanIds,
} = await import(moduleUrl);

function span(spanId, parentSpanId, name, start) {
  return { span_id: spanId, parent_span_id: parentSpanId, name, start_time: start };
}

test('children listed before parent still nest correctly', () => {
  const { roots, byId } = buildSpanTree([
    span('c2', 'root', 'child-2', '2026-01-01T00:00:02Z'),
    span('c1', 'root', 'child-1', '2026-01-01T00:00:01Z'),
    span('root', null, 'agent', '2026-01-01T00:00:00Z'),
  ]);
  assert.deepEqual(roots.map((node) => node.span_id), ['root']);
  assert.deepEqual(roots[0].children.map((node) => node.span_id), ['c1', 'c2']);
  assert.equal(byId.get('root').children.length, 2);
});

test('orphan span is shown under a synthetic missing-parent group', () => {
  const { roots, byId } = buildSpanTree([
    span('orphan', 'gone', 'orphan', '2026-01-01T00:00:00Z'),
    span('child', 'orphan', 'child', '2026-01-01T00:00:01Z'),
  ]);
  assert.equal(roots.length, 1);
  assert.equal(roots[0].span_id, MISSING_PARENT_ID);
  assert.equal(roots[0].name, '(missing parent)');
  assert.deepEqual(roots[0].children.map((node) => node.span_id), ['orphan']);
  assert.deepEqual(roots[0].children[0].children.map((node) => node.span_id), ['child']);
  assert.equal(byId.has('orphan'), true);
});

test('cycle does not hang and is broken', () => {
  const originalWarn = console.warn;
  console.warn = () => {};
  try {
    const { roots } = buildSpanTree([
      span('a', 'b', 'a', '2026-01-01T00:00:00Z'),
      span('b', 'a', 'b', '2026-01-01T00:00:01Z'),
      span('self', 'self', 'self', '2026-01-01T00:00:02Z'),
    ]);
    assert.deepEqual(roots.map((node) => node.span_id), ['a', 'self']);
    assert.deepEqual(roots[0].children.map((node) => node.span_id), ['b']);
    assert.equal(roots[1].children.length, 0);
  } finally {
    console.warn = originalWarn;
  }
});

test('siblings sorted by start_time with span_id tie-break', () => {
  const { roots } = buildSpanTree([
    span('root', null, 'root', '2026-01-01T00:00:00Z'),
    span('b', 'root', 'tie', '2026-01-01T00:00:05Z'),
    span('a', 'root', 'tie', '2026-01-01T00:00:05Z'),
    span('early', 'root', 'early', '2026-01-01T00:00:01Z'),
  ]);
  assert.deepEqual(roots[0].children.map((node) => node.span_id), ['early', 'a', 'b']);
});

test('three-level nesting computes depth and isLast', () => {
  const { roots } = buildSpanTree([
    span('root', null, 'root', '2026-01-01T00:00:00Z'),
    span('a', 'root', 'a', '2026-01-01T00:00:01Z'),
    span('a1', 'a', 'a1', '2026-01-01T00:00:02Z'),
    span('a2', 'a', 'a2', '2026-01-01T00:00:03Z'),
    span('b', 'root', 'b', '2026-01-01T00:00:04Z'),
  ]);
  const rows = flattenVisibleSpans(roots);
  assert.deepEqual(rows.map((row) => row.node.span_id), ['root', 'a', 'a1', 'a2', 'b']);
  assert.deepEqual(rows.map((row) => row.depth), [0, 1, 2, 2, 1]);
  assert.deepEqual(rows.map((row) => row.isLast), [true, false, false, true, true]);
  assert.equal(countTreeNodes(roots), 5);
});

test('collapsing a node hides its whole subtree', () => {
  const { roots } = buildSpanTree([
    span('root', null, 'root', '2026-01-01T00:00:00Z'),
    span('a', 'root', 'a', '2026-01-01T00:00:01Z'),
    span('a1', 'a', 'a1', '2026-01-01T00:00:02Z'),
    span('b', 'root', 'b', '2026-01-01T00:00:03Z'),
  ]);
  const rows = flattenVisibleSpans(roots, { collapsed: new Set(['root']) });
  assert.deepEqual(rows.map((row) => row.node.span_id), ['root']);
  const partial = flattenVisibleSpans(roots, { collapsed: new Set(['a']) });
  assert.deepEqual(partial.map((row) => row.node.span_id), ['root', 'a', 'b']);
});

test('filter keeps ancestors of matching spans visible', () => {
  const { roots } = buildSpanTree([
    span('root', null, 'agent', '2026-01-01T00:00:00Z'),
    span('child', 'root', 'search', '2026-01-01T00:00:01Z'),
    span('other', null, 'model call', '2026-01-01T00:00:02Z'),
  ]);
  const filtered = filterSpanTree(roots, 'search');
  assert.deepEqual(filtered.map((node) => node.span_id), ['root']);
  assert.deepEqual(filtered[0].children.map((node) => node.span_id), ['child']);
  assert.deepEqual([...matchingSpanIds(roots, 'search')], ['child']);

  const rows = flattenVisibleSpans(roots, {
    collapsed: new Set(['root']),
    matches: matchingSpanIds(roots, 'search'),
  });
  assert.deepEqual(rows.map((row) => row.node.span_id), ['root', 'child', 'other']);
});

test('single-span trace renders one root', () => {
  const { roots, byId } = buildSpanTree([span('only', null, 'only', '2026-01-01T00:00:00Z')]);
  assert.equal(roots.length, 1);
  assert.equal(roots[0].span_id, 'only');
  assert.equal(byId.size, 1);
});
