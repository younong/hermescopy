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

export interface CompressionStatus {
  kind: CompressionStatusKind
  text: string
}

export const $compressionStatuses = atom<Record<string, CompressionStatus>>({})
export const $compactingSessions = computed($compressionStatuses, statuses =>
  Object.fromEntries(
    Object.entries(statuses)
      .filter(([, status]) => status.kind === 'compression.preparing')
      .map(([sessionId]) => [sessionId, true] as const)
  )
)

export const $compactionActive = computed(
  [$compactingSessions, $activeSessionId],
  (sessions, activeId) => keyFor(activeId) in sessions
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

    $compressionStatuses.set({ ...statuses, [key]: status })

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
