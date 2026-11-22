import { toFiniteNumber } from './format';

export interface RawTokenCounts {
  input: number | null;
  output: number | null;
  total: number | null;
}

export interface ResolvedTokens extends RawTokenCounts {
  inputEstimated: boolean;
  outputEstimated: boolean;
  totalEstimated: boolean;
  estimated: boolean;
}

type AnyRecord = Record<string, unknown>;

const UNIT = { word: 1.3, digit: 0.5, symbol: 0.8, cjk: 1.2, other: 1.8, emoji: 2.5 };
const FAMILY = { gpt: 1, claude: 1.1, gemini: 0.95, unknown: 1.05 };
const HISTORY_LIMIT = 50;

const TOKEN_INPUT_KEYS = new Set([
  'inputtokens',
  'prompttokens',
  'inputtokencount',
  'prompttokencount',
]);

const TOKEN_OUTPUT_KEYS = new Set([
  'outputtokens',
  'completiontokens',
  'outputtokencount',
  'completiontokencount',
]);

const TOKEN_TOTAL_KEYS = new Set(['totaltokens', 'tokencount', 'totaltokencount']);

const calibrationHistory = new Map<string, number[]>();
const learnedKeys = new Set<string>();

export const ESTIMATE_TOOLTIP = 'Estimated from text length. The provider did not report usage.';
export const OUTPUT_ESTIMATE_TOOLTIP =
  'Estimated from text length. The provider did not report usage. Hidden reasoning tokens are not in the output text, so output estimates for reasoning models are lower bounds.';
export const COST_ESTIMATE_TOOLTIP =
  'Estimated from token counts. The provider did not report cost.';

function hasValue(value: unknown): boolean {
  return value !== null && value !== undefined;
}

function pick(source: AnyRecord | null | undefined, keys: string[]): unknown {
  if (!source) return null;
  for (const key of keys) {
    const value = source[key];
    if (hasValue(value)) return value;
  }
  return null;
}

function tokenBucket(key: unknown): 'input' | 'output' | 'total' | null {
  const raw = String(key ?? '').trim().toLowerCase();
  const normalized = raw.replace(/[^a-z0-9]/g, '');
  if (TOKEN_INPUT_KEYS.has(normalized)) return 'input';
  if (TOKEN_OUTPUT_KEYS.has(normalized)) return 'output';
  if (TOKEN_TOTAL_KEYS.has(normalized)) return 'total';
  if (raw.startsWith('gen_ai.usage.')) {
    const suffix = raw.slice('gen_ai.usage.'.length).replace(/[^a-z0-9]/g, '');
    if (TOKEN_INPUT_KEYS.has(suffix)) return 'input';
    if (TOKEN_OUTPUT_KEYS.has(suffix)) return 'output';
    if (TOKEN_TOTAL_KEYS.has(suffix)) return 'total';
  }
  return null;
}

export function readTokenCounts(source: unknown): RawTokenCounts {
  const found: RawTokenCounts = { input: null, output: null, total: null };
  const seen = new Set<object>();
  const assign = (bucket: 'input' | 'output' | 'total', value: unknown) => {
    const number = toFiniteNumber(value);
    if (number !== null && found[bucket] === null) found[bucket] = number;
  };
  const visit = (value: unknown) => {
    if (!hasValue(value) || typeof value !== 'object') return;
    if (seen.has(value as object)) return;
    seen.add(value as object);
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    for (const [key, child] of Object.entries(value as AnyRecord)) {
      const bucket = tokenBucket(key);
      if (bucket) assign(bucket, child);
      visit(child);
    }
  };
  visit(source);
  return found;
}

function hash(value: string): number {
  let h = 1779033703 ^ value.length;
  for (let i = 0; i < value.length; i += 1) {
    h = Math.imul(h ^ value.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  return h >>> 0;
}

function seeded(seed: number): () => number {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function family(model: string): keyof typeof FAMILY {
  const value = model.toLowerCase();
  if (value.includes('claude')) return 'claude';
  if (value.includes('gpt') || /\bo[13]\b/.test(value)) return 'gpt';
  if (value.includes('gemini')) return 'gemini';
  return 'unknown';
}

function rawEstimate(text: string): number {
  const emoji = (text.match(/\p{Extended_Pictographic}/gu) || []).length;
  const cjk = (text.match(/[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]/g) || []).length;
  const nonLatin = (text.match(/[^\u0000-\u024f\s]/gu) || []).length;
  const other = Math.max(0, nonLatin - emoji - cjk);
  const digits = (text.match(/\d/g) || []).length;
  const symbols = (text.match(/[^\w\s\u0080-\uffff]/g) || []).length;
  const words = (text.match(/[A-Za-z]+/g) || []).length;
  return (
    words * UNIT.word +
    digits * UNIT.digit +
    symbols * UNIT.symbol +
    cjk * UNIT.cjk +
    other * UNIT.other +
    emoji * UNIT.emoji
  );
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

function textOf(value: unknown): string {
  if (!hasValue(value)) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value) ?? '';
  } catch {
    return String(value);
  }
}

function attributesOf(record: AnyRecord | null | undefined): AnyRecord {
  const value = record?.attributes;
  if (value && typeof value === 'object' && !Array.isArray(value)) return value as AnyRecord;
  return {};
}

function modelOf(record: AnyRecord | null | undefined): string {
  const attributes = attributesOf(record);
  const value = pick(record, ['model', 'model_name']) ?? pick(attributes, ['model', 'model_name']);
  return hasValue(value) ? String(value) : '';
}

function inputTextOf(record: AnyRecord | null | undefined): string {
  const attributes = attributesOf(record);
  const value = pick(record, ['input', 'prompt', 'messages']) ?? pick(attributes, ['input', 'prompt', 'messages']);
  return textOf(value);
}

function outputTextOf(record: AnyRecord | null | undefined): string {
  const attributes = attributesOf(record);
  const value = pick(record, ['output', 'response', 'result']) ?? pick(attributes, ['output', 'response', 'result']);
  return textOf(value);
}

function messageCountOf(record: AnyRecord | null | undefined): number {
  const attributes = attributesOf(record);
  const messages = pick(record, ['messages']) ?? pick(attributes, ['messages']);
  if (Array.isArray(messages)) return messages.length;
  const input = pick(record, ['input', 'prompt']) ?? pick(attributes, ['input', 'prompt']);
  return Array.isArray(input) ? input.length : 1;
}

function idOf(record: AnyRecord | null | undefined, fallback?: string): string {
  const value =
    pick(record, ['span_id', 'trace_id', 'id']) ?? fallback ?? textOf(record).slice(0, 128);
  return String(value);
}

function learn(model: string, text: string, realTokens: number): void {
  const raw = rawEstimate(text);
  if (raw < 5 || !realTokens) return;
  const key = model || 'unknown';
  const values = calibrationHistory.get(key) ?? [];
  values.push(realTokens / raw);
  if (values.length > HISTORY_LIMIT) values.shift();
  calibrationHistory.set(key, values);
}

function estimateTokens(
  text: string,
  id: string,
  kind: 'input' | 'output',
  model: string,
  messageCount = 1,
): number | null {
  if (!text) return null;
  const random = seeded(hash(`${id}:${kind}`));
  const jitter = 0.92 + random() * 0.16;
  const values = calibrationHistory.get(model || 'unknown');
  const calibration = values && values.length > 0 ? median(values) : 1;
  const overhead = kind === 'input' ? messageCount * 4 + 3 : 0;
  const value = (rawEstimate(text) + overhead) * FAMILY[family(model)] * calibration * jitter;
  return Math.max(1, Math.round(value));
}

function recordKey(record: AnyRecord | null | undefined, fallback?: string): string {
  const counts = readTokenCounts(record);
  return [
    idOf(record, fallback),
    modelOf(record),
    counts.input,
    counts.output,
    counts.total,
  ].join('|');
}

export function learnTokenHistory(records: ReadonlyArray<AnyRecord | null | undefined>): void {
  for (const record of records) {
    if (!record) continue;
    const counts = readTokenCounts(record);
    if (counts.input === null && counts.output === null && counts.total === null) continue;
    const key = recordKey(record);
    if (learnedKeys.has(key)) continue;
    learnedKeys.add(key);
    if (learnedKeys.size > 10000) learnedKeys.clear();
    const model = modelOf(record);
    if (counts.input !== null) learn(model, inputTextOf(record), counts.input);
    if (counts.output !== null) learn(model, outputTextOf(record), counts.output);
  }
}

export function reportedTokens(record: AnyRecord | null | undefined): ResolvedTokens {
  const counts = readTokenCounts(record);
  return { ...counts, inputEstimated: false, outputEstimated: false, totalEstimated: false, estimated: false };
}

export function resolveTokens(record: AnyRecord | null | undefined, fallbackId?: string): ResolvedTokens {
  const counts = readTokenCounts(record);
  const id = idOf(record, fallbackId);
  const model = modelOf(record);
  const input = counts.input ?? estimateTokens(inputTextOf(record), id, 'input', model, messageCountOf(record));
  const output = counts.output ?? estimateTokens(outputTextOf(record), id, 'output', model);
  const inputEstimated = counts.input === null && input !== null;
  const outputEstimated = counts.output === null && output !== null;
  const total =
    counts.total ?? (input !== null && output !== null ? input + output : input ?? output);
  const totalEstimated = counts.total === null && (inputEstimated || outputEstimated);
  return {
    input,
    output,
    total,
    inputEstimated,
    outputEstimated,
    totalEstimated,
    estimated: inputEstimated || outputEstimated || totalEstimated,
  };
}

export function resolveAggregateTokens(
  records: ReadonlyArray<AnyRecord | null | undefined>,
  fallbackId?: string,
): ResolvedTokens {
  learnTokenHistory(records);
  let input = 0;
  let output = 0;
  let total = 0;
  let hasInput = false;
  let hasOutput = false;
  let hasTotal = false;
  let inputEstimated = false;
  let outputEstimated = false;
  let totalEstimated = false;

  records.forEach((record, index) => {
    if (!record) return;
    const tokens = resolveTokens(record, fallbackId ? `${fallbackId}:${index}` : undefined);
    if (tokens.input !== null) {
      input += tokens.input;
      hasInput = true;
      inputEstimated = inputEstimated || tokens.inputEstimated;
    }
    if (tokens.output !== null) {
      output += tokens.output;
      hasOutput = true;
      outputEstimated = outputEstimated || tokens.outputEstimated;
    }
    if (tokens.total !== null) {
      total += tokens.total;
      hasTotal = true;
      totalEstimated = totalEstimated || tokens.totalEstimated;
    }
  });

  return {
    input: hasInput ? input : null,
    output: hasOutput ? output : null,
    total: hasTotal
      ? total
      : hasInput || hasOutput
        ? (hasInput ? input : 0) + (hasOutput ? output : 0)
        : null,
    inputEstimated,
    outputEstimated,
    totalEstimated: hasTotal ? totalEstimated : inputEstimated || outputEstimated,
    estimated: inputEstimated || outputEstimated || (hasTotal ? totalEstimated : inputEstimated || outputEstimated),
  };
}
