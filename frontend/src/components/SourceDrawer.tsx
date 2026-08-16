import { CloseIcon, FileIcon, GlobeIcon } from './Icons'
import type { Source } from '@/types'

interface SourceDrawerProps {
  source: Source | null
  onClose: () => void
}

/** Opens when a citation chip is clicked, so "where did this come from?" is one click. */
export function SourceDrawer({ source, onClose }: SourceDrawerProps) {
  if (!source) return null
  const isWeb = source.kind === 'web'

  return (
    <>
      <div className="drawer-scrim" onClick={onClose} aria-hidden />
      <aside className="drawer" role="dialog" aria-label="Source detail">
        <header className="drawer-head">
          <span className="drawer-index">{source.index}</span>
          <div className="drawer-title">
            {isWeb ? <GlobeIcon size={14} /> : <FileIcon size={14} />}
            <strong>{source.label}</strong>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close source">
            <CloseIcon />
          </button>
        </header>

        <div className="drawer-meta">
          {source.page_from && (
            <span>
              Page {source.page_from}
              {source.page_to && source.page_to !== source.page_from ? `–${source.page_to}` : ''}
            </span>
          )}
          {source.score !== null && source.score !== undefined && (
            <span>Relevance {source.score.toFixed(3)}</span>
          )}
          {source.modality === 'image' && <span>Image</span>}
        </div>

        <div className="drawer-body">
          <p>{source.snippet}</p>
        </div>

        {source.url && (
          <a className="drawer-link" href={source.url} target="_blank" rel="noopener noreferrer">
            Open original ↗
          </a>
        )}
      </aside>
    </>
  )
}
