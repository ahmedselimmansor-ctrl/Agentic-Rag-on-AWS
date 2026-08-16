import { Children, isValidElement, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import type { Source } from '@/types'

const CITATION = /\[(\d{1,2})\]/g

/**
 * Turns inline `[3]` markers into clickable chips bound to the source list.
 * Runs over rendered children rather than the raw string so markers inside code
 * blocks and links are left alone.
 */
function linkCitations(
  children: ReactNode,
  sources: Source[],
  onCite?: (source: Source) => void,
): ReactNode {
  if (!sources.length) return children

  return Children.map(children, (child) => {
    if (typeof child === 'number') return child
    if (typeof child !== 'string') {
      // Never rewrite inside code — `arr[1]` is not a citation.
      if (isValidElement(child) && (child.type === 'code' || child.type === 'pre')) return child
      return child
    }

    const parts: ReactNode[] = []
    let cursor = 0
    let match: RegExpExecArray | null
    CITATION.lastIndex = 0

    while ((match = CITATION.exec(child)) !== null) {
      const index = Number(match[1])
      const source = sources.find((s) => s.index === index)
      if (!source) continue

      if (match.index > cursor) parts.push(child.slice(cursor, match.index))
      parts.push(
        <button
          key={`${match.index}-${index}`}
          className="citation"
          onClick={() => onCite?.(source)}
          title={source.label}
          type="button"
        >
          {index}
        </button>,
      )
      cursor = match.index + match[0].length
    }

    if (!parts.length) return child
    if (cursor < child.length) parts.push(child.slice(cursor))
    return parts
  })
}

interface MarkdownProps {
  content: string
  sources?: Source[]
  onCite?: (source: Source) => void
}

export function Markdown({ content, sources = [], onCite }: MarkdownProps) {
  const withCitations = (children: ReactNode) => linkCitations(children, sources, onCite)

  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
        components={{
          p: ({ children }) => <p>{withCitations(children)}</p>,
          li: ({ children }) => <li>{withCitations(children)}</li>,
          td: ({ children }) => <td>{withCitations(children)}</td>,
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
          pre: ({ children }) => (
            <div className="code-block">
              <pre>{children}</pre>
            </div>
          ),
          table: ({ children }) => (
            <div className="table-scroll">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
