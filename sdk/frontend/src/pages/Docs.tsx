import { useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import Icon from '../components/Icons';

type CodeTab = { label: string; code: string };

type DocsBlock =
  | { type: 'paragraph'; text: string }
  | { type: 'list'; items: string[] }
  | { type: 'steps'; items: string[] }
  | { type: 'code'; title?: string; tabs: CodeTab[] }
  | { type: 'table'; columns: string[]; rows: string[][] }
  | { type: 'diagram'; nodes: string[]; caption: string }
  | {
      type: 'cards';
      items: { title: string; text: string; href: string }[];
    }
  | { type: 'note'; text: string; todo?: boolean };

type DocsSection = {
  id: string;
  title: string;
  summary: string;
  blocks: DocsBlock[];
};

const SECTIONS: DocsSection[] = [
  {
    id: 'what-is-tracekit',
    title: 'What is TraceKit',
    summary: 'Local LLM tracing for Python apps.',
    blocks: [
      {
        type: 'paragraph',
        text: 'TraceKit records traces, spans, and events from your Python app into a SQLite file on your machine. You can inspect that data in the terminal or open the local dashboard. This repository does not include a hosted service, and the default path does not need a separate collector.',
      },
      {
        type: 'list',
        items: [
          'A trace is one request, job, or run you want to follow.',
          'A span is a unit of work inside that trace, such as an LLM call or a tool call.',
          'Events and attributes sit on the span where they happened.',
          'Projects and API keys live in the same local database.',
        ],
      },
      {
        type: 'paragraph',
        text: 'The SDK also supports W3C trace context, sampling strategies, and LangChain callbacks. Those features write to the same local store.',
      },
    ],
  },
  {
    id: 'how-it-works',
    title: 'How it works',
    summary: 'The SDK writes. The CLI and dashboard read.',
    blocks: [
      {
        type: 'diagram',
        nodes: ['Your app', 'TraceKit Python SDK', 'SQLite on disk', 'Local dashboard'],
        caption: 'The SDK writes traces and spans straight to SQLite. The dashboard reads that file and serves the UI on 127.0.0.1 by default. OTLP exporters are optional.',
      },
      {
        type: 'steps',
        items: [
          'Your code creates a tracer and opens a trace.',
          'Spans record calls, inputs, outputs, events, and errors.',
          'The SDK saves the finished spans to the SQLite file.',
          'The CLI filters that file. The dashboard reads the same file through its API.',
        ],
      },
    ],
  },
  {
    id: 'quick-start',
    title: 'Quick start',
    summary: 'Install it, trace one block, open the dashboard.',
    blocks: [
      {
        type: 'code',
        title: 'Install from this repository',
        tabs: [
          {
            label: 'Terminal',
            code: 'python -m venv venv\nsource venv/bin/activate\npip install -e .',
          },
        ],
      },
      {
        type: 'code',
        title: 'Trace one operation',
        tabs: [
          {
            label: 'Python',
            code: 'from tracekit import Tracer\n\ntracer = Tracer()\nwith tracer.trace("example"):\n    with tracer.span("work", span_type="internal", input={"x": 1}) as span:\n        span.add_event("checkpoint", {"step": 1})\n        span.output = {"y": 2}',
          },
        ],
      },
      {
        type: 'paragraph',
        text: 'Run your app, then use the CLI to list traces or open the dashboard.',
      },
      {
        type: 'code',
        tabs: [
          {
            label: 'Terminal',
            code: 'tracekit list\ntracekit dashboard --open',
          },
        ],
      },
      {
        type: 'paragraph',
        text: 'When you do not pass `--token`, the dashboard prints a URL with a random token. Use that URL to open the UI.',
      },
    ],
  },
  {
    id: 'cli',
    title: 'CLI commands',
    summary: 'Filter and inspect traces from the terminal.',
    blocks: [
      {
        type: 'paragraph',
        text: 'Every command accepts `--db`. Output is `table` by default. Add `--output json` when you want records to pipe into another tool.',
      },
      {
        type: 'table',
        columns: ['Command', 'What it does'],
        rows: [
          [
            '`tracekit list`',
            'Lists recent traces. Filters include status, span type, name, project, environment, error type, time range, and latency.',
          ],
          ['`tracekit search`', 'Uses the same filters as `list`.'],
          ['`tracekit watch`', 'Polls for new traces. The default interval is 2 seconds.'],
          ['`tracekit show TRACE_ID`', 'Prints one trace and its spans.'],
          [
            '`tracekit replay TRACE_ID`',
            'Prints the saved replay tree. It reads stored spans and does not call the model again.',
          ],
          [
            '`tracekit delete --older-than 7d --yes`',
            'Deletes by trace id or age. Deletion needs `--yes`.',
          ],
          [
            '`tracekit projects`',
            'Lists, shows, creates, archives, and deletes projects.',
          ],
          [
            '`tracekit api-keys`',
            'Creates, validates, lists, and revokes API keys.',
          ],
          [
            '`tracekit dashboard --open`',
            'Serves the local UI on `127.0.0.1:8766` by default.',
          ],
        ],
      },
      {
        type: 'code',
        title: 'A few useful calls',
        tabs: [
          {
            label: 'Terminal',
            code: 'tracekit list --status error --limit 20 --output json\ntracekit show TRACE_ID\ntracekit dashboard --port 8767 --open',
          },
        ],
      },
    ],
  },
  {
    id: 'local-setup',
    title: 'Local setup',
    summary: 'The SQLite file, environment variables, and dashboard address.',
    blocks: [
      {
        type: 'paragraph',
        text: 'The default data folder is `~/.tracellm`. The default SQLite file is `~/.tracellm/traces.db`. The sidebar footer shows the resolved path from the dashboard API.',
      },
      {
        type: 'paragraph',
        text: 'Configuration comes from process environment variables. The config loader also supports a `.env` file in the current directory when it runs with the default path. It reads simple `KEY=value` lines, and process variables win over `.env` values.',
      },
      {
        type: 'table',
        columns: ['Variable', 'Default', 'Use'],
        rows: [
          [
            '`TRACEKIT_DB_PATH`',
            'none',
            'Full SQLite path. It takes precedence over `TRACELLM_HOME`.',
          ],
          [
            '`TRACELLM_HOME`',
            '`~/.tracellm`',
            'Base folder for `traces.db`.',
          ],
          [
            '`TRACEKIT_ENVIRONMENT`',
            '`development`',
            'Environment name stored on traces.',
          ],
          ['`TRACEKIT_LOG_LEVEL`', '`INFO`', 'Log level.'],
          ['`TRACEKIT_SERVICE_NAME`', '`tracekit`', 'OTLP resource service name.'],
          ['`TRACEKIT_AUTO_FLUSH`', '`false`', 'Turns on automatic flushing.'],
          ['`TRACEKIT_FLUSH_INTERVAL_SECONDS`', '`30`', 'Flush interval in seconds.'],
          ['`TRACEKIT_MAX_SPANS_IN_MEMORY`', '`1000`', 'Spans held in memory before flushing.'],
          ['`TRACEKIT_SAMPLING_RATE`', '`1.0`', 'Default sampling rate.'],
          [
            '`TRACEKIT_TOKEN_ESTIMATION_JITTER`',
            '`0.10`',
            'Jitter applied to token estimates.',
          ],
          ['`OTLP_ENDPOINT`', 'none', 'Optional OTLP/HTTP export endpoint.'],
          [
            '`OTLP_HEADERS`',
            'none',
            'Comma-separated `key=value` headers for OTLP export.',
          ],
        ],
      },
      {
        type: 'paragraph',
        text: 'The dashboard binds to `127.0.0.1` and listens on `8766`. It prints a URL with a random token. Use `--host`, `--port`, `--token`, and `--open` to change that.',
      },
      {
        type: 'code',
        tabs: [
          {
            label: 'Terminal',
            code: 'cp .env.example .env\ntracekit dashboard --db ~/.tracellm/traces.db --port 8767 --open',
          },
        ],
      },
      {
        type: 'note',
        todo: true,
        text: 'TODO: confirm whether `TRACEKIT_STREAM_ENABLED`, `TRACEKIT_STREAM_HOST`, `TRACEKIT_STREAM_PORT`, and `TRACEKIT_WEBHOOK_ENDPOINT` are read by the runtime. They appear in `.env.example`, but the config loader checked here does not read those names.',
      },
    ],
  },
  {
    id: 'using-in-your-app',
    title: 'Using in your app',
    summary: 'Python is the shipped SDK in this repository.',
    blocks: [
      {
        type: 'paragraph',
        text: 'TraceKit has one tracing SDK here: Python 3.10 and newer. The TypeScript code under `sdk/frontend` builds the dashboard, not a tracing client. LangChain is the verified integration. Dedicated Gemini and Claude SDKs are not in this repository.',
      },
      {
        type: 'code',
        title: 'Python',
        tabs: [
          {
            label: 'Python',
            code: 'from tracekit import Tracer\n\ntracer = Tracer()\nwith tracer.trace("answer-question"):\n    with tracer.span("llm.call", span_type="llm", input={"prompt": "Hi"}) as span:\n        span.set_attribute("model", "gpt-4o-mini")\n        span.output = {"text": "Hello"}',
          },
        ],
      },
      {
        type: 'paragraph',
        text: 'For LangChain, install `tracekit[langchain]` and enable auto-instrumentation with `trace_langchain()` before you create your tools, models, and chains.',
      },
      {
        type: 'code',
        tabs: [
          {
            label: 'Python',
            code: 'from tracekit import Tracer, trace_langchain, untrace_langchain\n\ntracer = Tracer(project_name="my-app")\ntracer = trace_langchain(tracer)\n\nuntrace_langchain()',
          },
        ],
      },
    ],
  },
  {
    id: 'dashboard-guide',
    title: 'Dashboard guide',
    summary: 'Where to look after traces start landing.',
    blocks: [
      {
        type: 'cards',
        items: [
          {
            title: 'Traces',
            text: 'Filter recent traces, then open one to see its waterfall, input, output, events, attributes, and raw data.',
            href: '#/traces',
          },
          {
            title: 'Overview',
            text: 'Check request volume, errors, latency, token counts, and cost across the current filter.',
            href: '#/overview',
          },
          {
            title: 'Analytics',
            text: 'Look for patterns by model, provider, and time bucket.',
            href: '#/analytics',
          },
          {
            title: 'Errors',
            text: 'Start with failed traces and work down to the span that raised the error.',
            href: '#/errors',
          },
          {
            title: 'Projects',
            text: 'See project records and jump back to project-scoped traces.',
            href: '#/projects',
          },
        ],
      },
    ],
  },
  {
    id: 'troubleshooting',
    title: 'Troubleshooting',
    summary: 'Common blockers and what to check first.',
    blocks: [
      {
        type: 'list',
        items: [
          'If the dashboard says port 8766 is in use, choose another port: `tracekit dashboard --port 8767 --open`.',
          'If the dashboard shows no traces, compare the database path in the sidebar with the path used by your SDK and CLI. Also check the sampling rate and whether spans have been flushed.',
          'If an API call returns 403, open the URL printed by `tracekit dashboard` or send the token as a bearer token. `/api/` routes require it.',
          'If replay returns 422 with `no replay handler registered for provider`, register a handler for that provider. `tracekit replay TRACE_ID` still prints the stored tree.',
          'If token counts are estimated, the provider did not report usage. The dashboard derives them from text length and leaves reported counts alone.',
        ],
      },
      {
        type: 'paragraph',
        text: 'Replay execution can create a comparison trace. It needs a registered handler for the provider named on the original trace.',
      },
    ],
  },
];

const SECTION_IDS = new Set(SECTIONS.map((section) => section.id));

async function copyDocsText(value: string): Promise<boolean> {
  let clipboardOk = false;
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(value);
      clipboardOk = true;
    } catch {
      clipboardOk = false;
    }
  }
  if (clipboardOk) return true;
  try {
    const area = document.createElement('textarea');
    area.value = value;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand('copy');
    document.body.removeChild(area);
    return copied;
  } catch {
    return false;
  }
}

function renderInline(text: string): ReactNode {
  return text.split(/(`[^`]+`)/g).map((part, index) => {
    if (part.startsWith('`') && part.endsWith('`')) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }
    return <span key={index}>{part}</span>;
  });
}

function blockSearchText(block: DocsBlock): string {
  switch (block.type) {
    case 'paragraph':
    case 'note':
      return block.text;
    case 'list':
    case 'steps':
      return block.items.join(' ');
    case 'code':
      return block.tabs.map((tab) => `${tab.label} ${tab.code}`).join(' ');
    case 'table':
      return `${block.columns.join(' ')} ${block.rows.flat().join(' ')}`;
    case 'diagram':
      return `${block.nodes.join(' ')} ${block.caption}`;
    case 'cards':
      return block.items.map((item) => `${item.title} ${item.text}`).join(' ');
  }
}

function sectionSearchText(section: DocsSection): string {
  return [section.title, section.summary, ...section.blocks.map(blockSearchText)]
    .join(' ')
    .toLowerCase();
}

interface CodeBlockProps {
  blockKey: string;
  title?: string;
  tabs: CodeTab[];
  activeTab: string;
  copied: boolean;
  onTabChange: (label: string) => void;
  onCopy: () => void;
}

function CodeBlock({
  title,
  tabs,
  activeTab,
  copied,
  onTabChange,
  onCopy,
}: CodeBlockProps) {
  const active = tabs.find((tab) => tab.label === activeTab) ?? tabs[0];

  return (
    <div className="docs-code-card">
      <div className="docs-code-head">
        <div className="docs-code-title mono">{title ?? 'Terminal'}</div>
        <div className="docs-code-actions">
          <div className="docs-code-tabs" role="tablist" aria-label="Code language">
            {tabs.map((tab) => (
              <button
                key={tab.label}
                type="button"
                role="tab"
                aria-selected={tab.label === active.label}
                className={tab.label === active.label ? 'docs-code-tab active' : 'docs-code-tab'}
                onClick={() => onTabChange(tab.label)}
              >
                {tab.label}
              </button>
            ))}
          </div>
          <button type="button" className="docs-copy" onClick={onCopy}>
            <Icon name="copy" size={13} />
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>
      <pre className="docs-code mono">
        <code>{active.code}</code>
      </pre>
    </div>
  );
}

interface DocsProps {
  sectionId?: string;
}

export default function Docs({ sectionId }: DocsProps) {
  const [query, setQuery] = useState('');
  const [activeId, setActiveId] = useState(() =>
    sectionId && SECTION_IDS.has(sectionId) ? sectionId : SECTIONS[0].id,
  );
  const [menuOpen, setMenuOpen] = useState(false);
  const [activeTabs, setActiveTabs] = useState<Record<string, string>>({});
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const sectionRefs = useRef<Record<string, HTMLElement | null>>({});
  const copyTimerRef = useRef<number | null>(null);

  const visibleSections = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return SECTIONS;
    return SECTIONS.filter((section) => sectionSearchText(section).includes(needle));
  }, [query]);

  useEffect(() => {
    if (!sectionId || !SECTION_IDS.has(sectionId)) return;
    setActiveId(sectionId);
    const target = sectionRefs.current[sectionId];
    if (!target) return;
    const frame = window.requestAnimationFrame(() => target.scrollIntoView({ block: 'start' }));
    return () => window.cancelAnimationFrame(frame);
  }, [sectionId, query]);

  useEffect(() => {
    if (visibleSections.length === 0) return;
    if (!visibleSections.some((section) => section.id === activeId)) {
      setActiveId(visibleSections[0].id);
    }
  }, [activeId, visibleSections]);

  useEffect(() => {
    const elements = visibleSections
      .map((section) => sectionRefs.current[section.id])
      .filter((element): element is HTMLElement => Boolean(element));
    if (elements.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const inView = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        const first = inView[0];
        if (!first) return;
        const id = first.target.id.replace(/^docs-/, '');
        if (SECTION_IDS.has(id)) setActiveId(id);
      },
      { rootMargin: '-88px 0px -62% 0px', threshold: 0 },
    );
    elements.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [visibleSections]);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    };
  }, []);

  const handleCopy = async (blockKey: string, code: string) => {
    const ok = await copyDocsText(code);
    if (!ok) return;
    setCopiedKey(blockKey);
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    copyTimerRef.current = window.setTimeout(() => setCopiedKey(null), 1600);
  };

  const renderBlock = (block: DocsBlock, blockKey: string): ReactNode => {
    switch (block.type) {
      case 'paragraph':
        return <p>{renderInline(block.text)}</p>;
      case 'list':
        return (
          <ul className="docs-list">
            {block.items.map((item, index) => (
              <li key={index}>{renderInline(item)}</li>
            ))}
          </ul>
        );
      case 'steps':
        return (
          <ol className="docs-steps">
            {block.items.map((item, index) => (
              <li key={index}>{renderInline(item)}</li>
            ))}
          </ol>
        );
      case 'code': {
        const activeTab = activeTabs[blockKey] ?? block.tabs[0].label;
        return (
          <CodeBlock
            blockKey={blockKey}
            title={block.title}
            tabs={block.tabs}
            activeTab={activeTab}
            copied={copiedKey === blockKey}
            onTabChange={(label) => setActiveTabs((current) => ({ ...current, [blockKey]: label }))}
            onCopy={() => void handleCopy(blockKey, block.tabs.find((tab) => tab.label === activeTab)?.code ?? block.tabs[0].code)}
          />
        );
      }
      case 'table':
        return (
          <div className="docs-table-wrap">
            <table className="docs-table">
              <thead>
                <tr>
                  {block.columns.map((column) => (
                    <th key={column}>{column}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {row.map((cell, cellIndex) => (
                      <td key={cellIndex}>{renderInline(cell)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      case 'diagram':
        return (
          <div className="docs-diagram">
            <div className="docs-diagram-flow">
              {block.nodes.map((node, index) => (
                <div className="docs-diagram-part" key={node}>
                  <div className="docs-diagram-node">{node}</div>
                  {index < block.nodes.length - 1 && <div className="docs-diagram-arrow">→</div>}
                </div>
              ))}
            </div>
            <p className="docs-diagram-caption">{renderInline(block.caption)}</p>
          </div>
        );
      case 'cards':
        return (
          <div className="docs-cards">
            {block.items.map((item) => (
              <a className="docs-card" href={item.href} key={item.title}>
                <span className="docs-card-title">{item.title}</span>
                <span className="docs-card-text">{renderInline(item.text)}</span>
                <span className="docs-card-arrow" aria-hidden="true">
                  →
                </span>
              </a>
            ))}
          </div>
        );
      case 'note':
        return (
          <div className={block.todo ? 'docs-note todo' : 'docs-note'}>
            {renderInline(block.text)}
          </div>
        );
    }
  };

  return (
    <div className="docs-page">
      <div className="docs-page-head">
        <div>
          <h1>Docs</h1>
          <p>How TraceKit stores traces, which commands read them, and where to look in the dashboard.</p>
        </div>
        <label className="docs-search">
          <Icon name="search" size={15} />
          <input
            type="search"
            value={query}
            placeholder="Search docs..."
            aria-label="Search docs"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>

      <div className={menuOpen ? 'docs-shell menu-open' : 'docs-shell'}>
        <button
          type="button"
          className="docs-menu-toggle"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((value) => !value)}
        >
          On this page
          <Icon name="chevron" size={14} />
        </button>

        <aside className="docs-toc" aria-label="On this page">
          <div className="docs-toc-title">On this page</div>
          {visibleSections.map((section) => (
            <a
              key={section.id}
              href={`#/docs/${section.id}`}
              className={activeId === section.id ? 'docs-toc-link active' : 'docs-toc-link'}
              aria-current={activeId === section.id ? 'location' : undefined}
              onClick={() => setMenuOpen(false)}
            >
              {section.title}
            </a>
          ))}
          {visibleSections.length === 0 && <p className="docs-empty-small">No sections match.</p>}
        </aside>

        <div className="docs-content">
          {visibleSections.map((section) => (
            <section
              key={section.id}
              id={`docs-${section.id}`}
              ref={(element) => {
                sectionRefs.current[section.id] = element;
              }}
              className="docs-section"
            >
              <h2>{section.title}</h2>
              <p className="docs-summary">{section.summary}</p>
              {section.blocks.map((block, index) => (
                <div className="docs-block" key={`${section.id}-${index}`}>
                  {renderBlock(block, `${section.id}-${index}`)}
                </div>
              ))}
            </section>
          ))}
          {visibleSections.length === 0 && (
            <div className="docs-empty">No docs sections match that search.</div>
          )}
        </div>
      </div>
    </div>
  );
}
