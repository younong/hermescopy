import { atom, computed } from 'nanostores'

import { $activeSessionId } from './session'

// Per-session transient compression state. Keeping the structured kind lets the
// thread distinguish active preparation from degraded/blocked guidance without
// ever inserting lifecycle text into the transcript.
const keyFor = (sessionId: string | null | undefined): string => sessionId ?? ''

export type CompressionStatusKind =
  | 'compression.blocked'
  | 'compression.cooldown'
  | 'compression.degraded'
  | 'compression.preparing'
  | 'compression.ready'

export interface CompressionStatus {
  kind: CompressionStatusKind
  text: string
  startedAt?: number
}

export const $compressionStatuses = atom<Record<string, CompressionStatus>>({})
export const $compactingSessions = computed($compressionStatuses, statuses =>
  Object.fromEntries(
    Object.entries(statuses)
      .filter(
        ([, status]) =>
          status.kind === 'compression.preparing' || status.kind === 'compression.ready'
      )
      .map(([sessionId]) => [sessionId, true] as const)
  )
)

export const $compactionActive = computed(
  [$compactingSessions, $activeSessionId],
  (sessions, activeId) => keyFor(activeId) in sessions
)

export const $activeCompressionStatus = computed(
  [$compressionStatuses, $activeSessionId],
  (statuses, activeId) => statuses[keyFor(activeId)]
)

export function setSessionCompressionStatus(
  sessionId: string | null | undefined,
  status?: CompressionStatus
): void {
  const key = keyFor(sessionId)

  if (!key) {
    return
  }

  const statuses = $compressionStatuses.get()

  if (status) {
    const current = statuses[key]

    if (current?.kind === status.kind && current.text === status.text) {
      return
    }

    const active = status.kind === 'compression.preparing' || status.kind === 'compression.ready'
    $compressionStatuses.set({
      ...statuses,
      [key]: {
        ...status,
        startedAt: active ? (current?.startedAt ?? status.startedAt ?? Date.now()) : undefined
      }
    })

    return
  }

  if (!(key in statuses)) {
    return
  }

  const next = { ...statuses }
  delete next[key]
  $compressionStatuses.set(next)
}

export function setSessionCompacting(sessionId: string | null | undefined, active: boolean): void {
  setSessionCompressionStatus(
    sessionId,
    active ? { kind: 'compression.preparing', text: 'Summarizing thread' } : undefined
  )
}
