import type { SpanNode } from '../types';

export const MISSING_PARENT_ID = '__tracekit_missing_parent__';

export interface SpanTreeNode extends SpanNode {
  children: SpanTreeNode[];
  depth: number;
  isLast: boolean;
  synthetic?: boolean;
}

export interface SpanTreeResult {
  roots: SpanTreeNode[];
  byId: Map<string, SpanTreeNode>;
}

export interface SpanTreeRow {
  node: SpanTreeNode;
  depth: number;
  isLast: boolean;
  guides: boolean[];
  hasChildren: boolean;
}

export interface FlattenOptions {
  collapsed?: Set<string>;
  matches?: Set<string> | null;
}

function toMillis(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value !== 'string' || value.trim() === '') return null;
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return numeric;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function idOf(node: SpanNode): string {
  return node.span_id ? String(node.span_id) : '';
}

function parentIdOf(node: SpanNode): string {
  const value = node.parent_span_id;
  return value === null || value === undefined ? '' : String(value).trim();
}

function startTime(node: SpanNode): number {
  return toMillis(node.start_time) ?? 0;
}

function sortKey(node: SpanNode): string {
  return idOf(node) || String(node.name ?? '');
}

function byStart(a: SpanNode, b: SpanNode): number {
  return startTime(a) - startTime(b) || sortKey(a).localeCompare(sortKey(b));
}

function cloneNode(node: SpanNode): SpanTreeNode {
  return {
    ...node,
    children: Array.isArray((node as SpanTreeNode).children)
      ? (node as SpanTreeNode).children.map(cloneNode)
      : [],
    depth: 0,
    isLast: true,
  } as SpanTreeNode;
}

function syntheticGroup(): SpanTreeNode {
  return {
    span_id: MISSING_PARENT_ID,
    trace_id: '',
    parent_span_id: null,
    name: '(missing parent)',
    span_type: 'other',
    status: 'warning',
    start_time: null,
    end_time: null,
    duration_ms: 0,
    children: [],
    depth: 0,
    isLast: true,
    synthetic: true,
  } as SpanTreeNode;
}

function sortTree(nodes: SpanTreeNode[]): void {
  nodes.sort(byStart);
  for (const node of nodes) sortTree(node.children);
}

function decorate(nodes: SpanTreeNode[], depth = 0): void {
  nodes.forEach((node, index) => {
    node.depth = depth;
    node.isLast = index === nodes.length - 1;
    decorate(node.children, depth + 1);
  });
}

export function countTreeNodes(nodes: SpanTreeNode[]): number {
  return nodes.reduce((total, node) => total + 1 + countTreeNodes(node.children), 0);
}

export function buildSpanTree(spans: SpanNode[]): SpanTreeResult {
  const byId = new Map<string, SpanTreeNode>();
  const nodes: SpanTreeNode[] = [];
  for (const span of Array.isArray(spans) ? spans : []) {
    const id = idOf(span);
    if (!id || byId.has(id)) continue;
    const node = cloneNode(span);
    node.children = [];
    nodes.push(node);
    byId.set(id, node);
  }

  const parentById = new Map<string, string>();
  for (const node of nodes) {
    const parentId = parentIdOf(node);
    if (parentId && byId.has(parentId)) parentById.set(idOf(node), parentId);
  }

  const checked = new Set<string>();
  const broken = new Set<string>();
  for (const id of byId.keys()) {
    if (checked.has(id)) continue;
    const path: string[] = [];
    const seen = new Set<string>();
    let cursor: string | undefined = id;
    while (cursor && parentById.has(cursor)) {
      checked.add(cursor);
      if (seen.has(cursor)) {
        const cycle = path.slice(path.indexOf(cursor));
        console.warn(`Span hierarchy cycle detected: ${cycle.join(' -> ')}; breaking the cycle`);
        broken.add(cursor);
        break;
      }
      seen.add(cursor);
      path.push(cursor);
      cursor = parentById.get(cursor);
    }
  }

  const roots: SpanTreeNode[] = [];
  let orphanGroup: SpanTreeNode | null = null;
  for (const node of nodes) {
    const parentId = parentIdOf(node);
    if (!parentId) {
      roots.push(node);
      continue;
    }
    if (broken.has(idOf(node))) {
      node.parent_span_id = null;
      roots.push(node);
      continue;
    }
    const parent = parentById.has(idOf(node)) ? byId.get(parentId) : undefined;
    if (parent) {
      parent.children.push(node);
      continue;
    }
    if (!orphanGroup) {
      orphanGroup = syntheticGroup();
      roots.push(orphanGroup);
    }
    orphanGroup.children.push(node);
  }

  sortTree(roots);
  decorate(roots);
  return { roots, byId };
}

export function normalizeSpanTree(nodes: SpanNode[]): SpanTreeNode[] {
  if (!Array.isArray(nodes)) return [];
  const roots = nodes.map(cloneNode);
  sortTree(roots);
  decorate(roots);
  return roots;
}

export function collectSpanIds(nodes: SpanTreeNode[], result: string[] = []): string[] {
  for (const node of nodes) {
    const id = idOf(node);
    if (id && id !== MISSING_PARENT_ID) result.push(id);
    collectSpanIds(node.children, result);
  }
  return result;
}

export interface SpanIndex {
  byId: Map<string, SpanTreeNode>;
  parentByChild: Map<string, string>;
  childCountById: Map<string, number>;
}

export function indexSpanTree(nodes: SpanTreeNode[]): SpanIndex {
  const byId = new Map<string, SpanTreeNode>();
  const parentByChild = new Map<string, string>();
  const childCountById = new Map<string, number>();
  const walk = (list: SpanTreeNode[], parentId: string | null) => {
    for (const node of list) {
      const id = idOf(node);
      if (id) {
        byId.set(id, node);
        if (parentId) parentByChild.set(id, parentId);
        childCountById.set(id, node.children.length);
      }
      walk(node.children, id || parentId);
    }
  };
  walk(nodes, null);
  return { byId, parentByChild, childCountById };
}

function searchText(node: SpanNode): string {
  const attributes =
    node.attributes && typeof node.attributes === 'object' && !Array.isArray(node.attributes)
      ? (node.attributes as Record<string, unknown>)
      : {};
  return [
    node.name,
    node.span_id,
    node.span_type,
    node.type,
    node.model,
    node.model_name,
    attributes.model,
    attributes.model_name,
  ]
    .filter((value) => value !== null && value !== undefined)
    .map((value) => String(value).toLowerCase())
    .join(' ');
}

export function filterSpanTree(nodes: SpanTreeNode[], query: string): SpanTreeNode[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return nodes;
  const result: SpanTreeNode[] = [];
  for (const node of nodes) {
    const children = filterSpanTree(node.children, needle);
    if (searchText(node).includes(needle) || children.length > 0) {
      const kept = cloneNode(node);
      kept.children = children;
      result.push(kept);
    }
  }
  decorate(result);
  return result;
}

export function matchingSpanIds(nodes: SpanTreeNode[], query: string): Set<string> {
  const needle = query.trim().toLowerCase();
  const result = new Set<string>();
  if (!needle) return result;
  const walk = (list: SpanTreeNode[]) => {
    for (const node of list) {
      if (searchText(node).includes(needle) && node.span_id && node.span_id !== MISSING_PARENT_ID) {
        result.add(String(node.span_id));
      }
      walk(node.children);
    }
  };
  walk(nodes);
  return result;
}

function subtreeHasMatch(node: SpanTreeNode, matches: Set<string>): boolean {
  if (node.span_id && matches.has(String(node.span_id))) return true;
  return node.children.some((child) => subtreeHasMatch(child, matches));
}

function shouldCollapse(node: SpanTreeNode, options: FlattenOptions): boolean {
  const id = idOf(node);
  const matches = options.matches;
  if (!id || !options.collapsed?.has(id)) return false;
  if (!matches) return true;
  return !node.children.some((child) => subtreeHasMatch(child, matches));
}

export function flattenVisibleSpans(
  nodes: SpanTreeNode[],
  options: FlattenOptions = {},
): SpanTreeRow[] {
  const rows: SpanTreeRow[] = [];
  const walk = (list: SpanTreeNode[], depth: number, guides: boolean[]) => {
    list.forEach((node, index) => {
      const hasNextSibling = index < list.length - 1;
      const collapsed = shouldCollapse(node, options);
      rows.push({
        node,
        depth,
        isLast: node.isLast,
        guides,
        hasChildren: node.children.length > 0,
      });
      if (node.children.length > 0 && !collapsed) {
        walk(node.children, depth + 1, [...guides, hasNextSibling]);
      }
    });
  };
  walk(nodes, 0, []);
  return rows;
}
