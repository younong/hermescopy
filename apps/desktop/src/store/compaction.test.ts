import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  $activeCompressionStatus,
  $compactingSessions,
  $compactionActive,
  $compressionStatuses,
  setSessionCompacting,
  setSessionCompressionStatus
} from './compaction'
import { $activeSessionId } from './session'

describe('compaction store', () => {
  beforeEach(() => {
    $compressionStatuses.set({})
    $activeSessionId.set(null)
  })

  afterEach(() => {
    $compressionStatuses.set({})
    $activeSessionId.set(null)
  })

  it('tracks compaction per session independently', () => {
    setSessionCompacting('session-a', true)
    setSessionCompacting('session-b', true)

    expect($compactingSessions.get()).toEqual({ 'session-a': true, 'session-b': true })
  })

  it('exposes only the active session via the focus-scoped view', () => {
    setSessionCompacting('session-a', true)

    expect($compactionActive.get()).toBe(false)

    $activeSessionId.set('session-a')
    expect($compactionActive.get()).toBe(true)

    $activeSessionId.set('session-b')
    expect($compactionActive.get()).toBe(false)
  })

  it('keeps preparing and ready active with one start time', () => {
    setSessionCompressionStatus('session-a', {
      kind: 'compression.preparing',
      startedAt: 123,
      text: 'Compressing context (1/2)…'
    })
    setSessionCompressionStatus('session-a', {
      kind: 'compression.ready',
      text: 'Applying summary…'
    })
    $activeSessionId.set('session-a')

    expect($activeCompressionStatus.get()).toEqual({
      kind: 'compression.ready',
      startedAt: 123,
      text: 'Applying summary…'
    })
    expect($compactionActive.get()).toBe(true)
  })

  it('keeps degraded and blocked states structured but not actively compacting', () => {
    setSessionCompressionStatus('session-a', {
      kind: 'compression.degraded',
      text: 'Compression paused; run /compress or /new'
    })

    expect($compressionStatuses.get()['session-a']).toEqual({
      kind: 'compression.degraded',
      text: 'Compression paused; run /compress or /new'
    })
    expect($compactingSessions.get()).toEqual({})
  })

  it('clears a session without disturbing the others', () => {
    setSessionCompacting('session-a', true)
    setSessionCompacting('session-b', true)

    setSessionCompacting('session-a', false)

    expect($compactingSessions.get()).toEqual({ 'session-b': true })
  })

  it('is a no-op when clearing an unknown session', () => {
    setSessionCompacting('session-a', true)
    const before = $compactingSessions.get()

    setSessionCompacting('session-missing', false)

    expect($compactingSessions.get()).toBe(before)
  })
})
