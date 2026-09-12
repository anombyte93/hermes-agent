/**
 * Aligned EVO drawer history — the runs / events / attachments sections paged
 * from the read-only /evidence/page bridge (one bounded cursor per resource),
 * plus authenticated attachment download through the existing JSON transport.
 *
 * Rendered ONLY when the selected board is identity-aligned with the EVO
 * database; the drawer decides that via /evidence/context and keeps its legacy
 * local detail (explicitly labelled) when alignment is false or unresolved.
 *
 * Identity is captured by board + card at call time: every page request carries
 * an explicit slug + card, query keys include both, and accumulation resets when
 * either changes — so a late response from a previous card can never land under
 * a new card's view. "Load more" appends the next cursor; exhaustion is stated.
 *
 * Download stays on the SAME authenticated REST door the desktop already uses
 * (`GET /attachments/{id}?format=json&board=slug`): base64 is decoded to a Blob
 * in the renderer and handed to an anchor download (object URL revoked after).
 * No direct renderer fetch, no token in a URL, no new binary IPC.
 */

import { Button, Codicon, useQuery, useValue } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'

import {
  $boardSlug,
  BOARDS_KEY,
  evidencePageKey,
  fetchAttachmentDownload,
  fetchBoards,
  fetchEvidencePage
} from './api'
import type { EvidenceEnvelope } from './types'
import { Callout, errText, Section, useKanban } from './ui'

type EvidenceResource = 'runs' | 'events' | 'attachments'

interface EvidencePageData {
  items: Array<Record<string, unknown>>
  returned: number
  has_more: boolean
  next_cursor?: null | string
  total?: number
  omitted?: number
}

/** The selected board's real slug, resolved from /boards.current when blank —
 *  the same rule the evidence bridge itself uses, so query keys and requests
 *  carry one explicit board identity. */
export function useResolvedBoardSlug(): string {
  const slug = useValue($boardSlug)
  const { data: boards } = useQuery({ queryKey: BOARDS_KEY, queryFn: fetchBoards, staleTime: 30_000 })

  return slug || boards?.current || ''
}

/** Decode the authenticated JSON payload to a Blob (real bytes, real Blob). */
export function base64ToBlob(contentBase64: string, contentType: string): Blob {
  const binary = atob(contentBase64)
  const bytes = new Uint8Array(binary.length)

  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i)
  }

  return new Blob([bytes], { type: contentType })
}

/** Hand a Blob to the OS download boundary: named anchor + object URL, revoked
 *  after the click so the renderer never leaks a blob reference. */
export function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')

  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

/** One bounded page per resource: accumulated rows, an explicit cursor, and a
 *  hard reset whenever the board or card identity changes. */
function usePagedResource(slug: string, card: string, resource: EvidenceResource) {
  const [cursor, setCursor] = useState<null | string>(null)
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([])
  const [exhausted, setExhausted] = useState(false)

  // Identity change (board switch / card switch / genuine refresh) resets the
  // accumulated pages so a stale page can never append under a new identity.
  useEffect(() => {
    setCursor(null)
    setRows([])
    setExhausted(false)
  }, [slug, card, resource])

  const query = useQuery({
    queryKey: evidencePageKey(slug, resource, card, cursor),
    queryFn: () => fetchEvidencePage(slug, resource, card, cursor, 50),
    enabled: !!slug && !!card,
    retry: false
  })

  const envelope = query.data as EvidenceEnvelope<EvidencePageData> | undefined
  const pageData = envelope?.state === 'PASS' ? (envelope.evidence ?? null) : null

  // Append the current page's rows once (deduped by id) and record exhaustion.
  useEffect(() => {
    if (!pageData || !Array.isArray(pageData.items)) {
      return
    }

    setRows(prev => {
      const seen = new Set(prev.map(row => String(row.id)))
      const fresh = pageData.items.filter(row => !seen.has(String(row.id)))

      return fresh.length ? [...prev, ...fresh] : prev
    })
    setExhausted(!pageData.has_more)
  }, [pageData])

  const hasMore = pageData ? pageData.has_more && !!pageData.next_cursor : false

  return {
    cursor,
    exhausted,
    hasMore,
    loadMore: () => setCursor(pageData?.next_cursor ?? null),
    pageData,
    query,
    rows
  }
}

function RunRow({ item }: { item: Record<string, unknown> }) {
  const outcome = item.outcome ?? item.status
  const failed = ['crashed', 'failed', 'timed_out', 'gave_up'].includes(String(outcome))

  return (
    <li className="flex flex-col gap-0.5 text-[0.71rem]">
      <div className="flex items-center gap-2">
        <span
          className="rounded px-1 py-px text-[0.625rem] font-medium"
          style={{ backgroundColor: failed ? 'var(--destructive, #f87171)' : 'var(--ui-bg-quaternary)' }}
        >
          {String(outcome ?? '')}
        </span>
        {item.profile ? <span className="text-(--ui-text-tertiary)">{String(item.profile)}</span> : null}
      </div>
      {Boolean(item.summary || item.error) && (
        <p className="line-clamp-2 whitespace-pre-wrap text-(--ui-text-quaternary)">{String(item.summary ?? item.error)}</p>
      )}
    </li>
  )
}

function EventRow({ item }: { item: Record<string, unknown> }) {
  return (
    <li className="flex items-baseline gap-2 text-[0.6875rem]">
      <span className="shrink-0 text-(--ui-text-secondary)">{String(item.kind ?? '').replace(/_/g, ' ')}</span>
    </li>
  )
}

function AttachmentRow({ slug, item }: { slug: string; item: Record<string, unknown> }) {
  const id = item.id as number | string
  const filename = String(item.filename ?? 'attachment')
  const size = typeof item.size === 'number' ? item.size : null
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<null | string>(null)

  const download = async () => {
    setPending(true)
    setError(null)

    try {
      const payload = await fetchAttachmentDownload(slug, id)
      triggerDownload(base64ToBlob(payload.content_base64, payload.content_type), payload.filename)
    } catch (err) {
      setError(errText(err))
    } finally {
      setPending(false)
    }
  }

  return (
    <li className="flex flex-col gap-1 text-[0.75rem]">
      <div className="flex items-center gap-1.5 text-(--ui-text-tertiary)">
        <Codicon name="file" size="0.75rem" />
        <span className="min-w-0 truncate">{filename}</span>
        {size != null && <span className="text-(--ui-text-quaternary)">{size} B</span>}
        <Button
          className="ml-auto"
          disabled={pending}
          onClick={() => void download()}
          size="xs"
          variant="outline"
        >
          <Codicon name={pending ? 'sync' : 'download'} size="0.75rem" spinning={pending} />
          Download
        </Button>
      </div>
      {error && (
        <p className="text-[0.6875rem] text-destructive">
          {error} — the attachment may have been removed. Re-upload to restore it.
        </p>
      )}
    </li>
  )
}

function EvidenceSection({ card, resource, slug }: { card: string; resource: EvidenceResource; slug: string }) {
  const k = useKanban()
  const { hasMore, loadMore, pageData, query, rows } = usePagedResource(slug, card, resource)

  const label =
    resource === 'runs' ? k.runs(rows.length) : resource === 'events' ? k.activity(rows.length) : k.attachments(rows.length)

  const name = resource === 'runs' ? 'runs' : resource === 'events' ? 'events' : 'attachments'

  if (query.isError) {
    return (
      <Section label={label}>
        <Callout title="Could not load" tone="var(--ui-text-quaternary)">
          <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
            Could not load {name} for this card. Retry on the next refresh.
          </p>
        </Callout>
      </Section>
    )
  }

  if (query.data && query.data.state !== 'PASS') {
    return (
      <Section label={label}>
        <Callout title="Unavailable" tone="var(--ui-text-quaternary)">
          <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
            {query.data.reason ?? `Evidence for ${name} is unavailable for this card.`}
          </p>
        </Callout>
      </Section>
    )
  }

  if (rows.length === 0) {
    return (
      <Section label={label}>
        <p className="text-[0.75rem] text-(--ui-text-quaternary)">No {name}.</p>
      </Section>
    )
  }

  return (
    <Section label={label}>
      <ul className="flex flex-col gap-1">
        {rows.map((item, index) =>
          resource === 'runs' ? (
            <RunRow item={item} key={String(item.id ?? index)} />
          ) : resource === 'events' ? (
            <EventRow item={item} key={String(item.id ?? index)} />
          ) : (
            <AttachmentRow item={item} key={String(item.id ?? index)} slug={slug} />
          )
        )}
      </ul>
      {typeof pageData?.omitted === 'number' && pageData.omitted > 0 && (
        <p className="text-[0.625rem] text-(--ui-text-quaternary)">+{pageData.omitted} omitted by the bounded page</p>
      )}
      {hasMore ? (
        <Button aria-label={`Load more ${name}`} disabled={query.isFetching} onClick={loadMore} size="xs" variant="outline">
          Load more
        </Button>
      ) : (
        <p className="text-[0.625rem] text-(--ui-text-quaternary)">All {name} loaded.</p>
      )}
    </Section>
  )
}

/** The aligned attachments section: paged rows + download, plus the SAME upload
 *  control the legacy drawer uses (the caller passes its existing mutation). */
function EvidenceAttachmentsSection({
  card,
  onUpload,
  slug,
  uploadPending
}: {
  card: string
  onUpload: (file: File) => void
  slug: string
  uploadPending: boolean
}) {
  const k = useKanban()
  const fileRef = useRef<HTMLInputElement>(null)
  const { hasMore, loadMore, pageData, query, rows } = usePagedResource(slug, card, 'attachments')

  return (
    <Section
      action={
        <>
          <input
            hidden
            onChange={event => {
              const file = event.target.files?.[0]

              if (file) {
                onUpload(file)
              }

              event.target.value = ''
            }}
            ref={fileRef}
            type="file"
          />
          <Button
            aria-label={k.uploadAttachment}
            disabled={uploadPending}
            onClick={() => fileRef.current?.click()}
            size="icon-xs"
            variant="ghost"
          >
            <Codicon name={uploadPending ? 'sync' : 'cloud-upload'} size="0.8rem" spinning={uploadPending} />
          </Button>
        </>
      }
      label={k.attachments(rows.length)}
    >
      {query.isError ? (
        <Callout title="Could not load" tone="var(--ui-text-quaternary)">
          <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
            Could not load attachments for this card. Retry on the next refresh.
          </p>
        </Callout>
      ) : query.data && query.data.state !== 'PASS' ? (
        <Callout title="Unavailable" tone="var(--ui-text-quaternary)">
          <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
            {query.data.reason ?? 'Evidence for attachments is unavailable for this card.'}
          </p>
        </Callout>
      ) : rows.length === 0 ? (
        <p className="text-[0.75rem] text-(--ui-text-quaternary)">{k.noAttachments}</p>
      ) : (
        <>
          <ul className="flex flex-col gap-1">
            {rows.map((item, index) => (
              <AttachmentRow item={item} key={String(item.id ?? index)} slug={slug} />
            ))}
          </ul>
          {typeof pageData?.omitted === 'number' && pageData.omitted > 0 && (
            <p className="text-[0.625rem] text-(--ui-text-quaternary)">+{pageData.omitted} omitted by the bounded page</p>
          )}
          {hasMore ? (
            <Button aria-label="Load more attachments" disabled={query.isFetching} onClick={loadMore} size="xs" variant="outline">
              Load more
            </Button>
          ) : (
            <p className="text-[0.625rem] text-(--ui-text-quaternary)">All attachments loaded.</p>
          )}
        </>
      )}
    </Section>
  )
}

/**
 * The aligned drawer history block: paged runs, paged events, and paged
 * attachments with download + upload. Rendered by the drawer only when the
 * board is EVO-aligned; the unaligned path keeps its legacy local detail.
 */
export function DrawerEvidence({
  id,
  onUpload,
  slug,
  uploadPending
}: {
  id: string
  onUpload: (file: File) => void
  slug: string
  uploadPending: boolean
}) {
  return (
    <>
      <EvidenceSection card={id} resource="runs" slug={slug} />
      <EvidenceSection card={id} resource="events" slug={slug} />
      <EvidenceAttachmentsSection card={id} onUpload={onUpload} slug={slug} uploadPending={uploadPending} />
    </>
  )
}
